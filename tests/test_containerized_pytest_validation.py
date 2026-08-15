"""Deterministic Docker pytest policy, provisioning, and lifecycle tests."""

from __future__ import annotations

import hashlib
import subprocess
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path

from pytest import MonkeyPatch, mark, raises

from agentic_sdlc.docker_validation import (
    DockerCliCommandRunner,
    DockerCommandResult,
    DockerPytestValidationExecutor,
    ValidationDependencyManifestError,
    build_python_dependency_manifest,
)
from agentic_sdlc.task_graph import (
    TaskValidationRequirement,
    ValidationExecutionProfile,
)
from agentic_sdlc.validation_execution import (
    ValidationExecutionInfrastructureCode,
    ValidationExecutionInfrastructureError,
    _CapturedOutput,
)
from agentic_sdlc.validation_execution_contracts import (
    PUBLIC_PYPI_INDEX_URL,
    GovernedValidationExecutionReport,
    PythonDependencyManifest,
    ValidationDependencyProvisioning,
    ValidationExecutionEnvironmentKind,
    ValidationExecutionOutcome,
    ValidationExecutionRequest,
    python_pytest_validation_policy,
    validation_execution_evidence_errors,
    validation_provisioning_evidence_errors,
)
from agentic_sdlc.workspace_runtime import (
    IsolatedWorkspace,
    create_isolated_workspace,
    snapshot_isolated_workspace,
)


_CONTAINER_ID = "a" * 64
_IMAGE_ID = "sha256:" + "b" * 64


def _captured(value: str, limit: int = 16 * 1024) -> _CapturedOutput:
    raw = value.encode()
    retained = raw[:limit]
    return _CapturedOutput(
        total_bytes=len(raw),
        retained_text=retained.decode(errors="replace"),
        sha256=hashlib.sha256(raw).hexdigest(),
        truncated=len(raw) > limit,
    )


def _command_result(
    argv: tuple[str, ...],
    *,
    exit_code: int | None = 0,
    stdout: str = "",
    stderr: str = "",
    timed_out: bool = False,
    limit: int = 16 * 1024,
) -> DockerCommandResult:
    return DockerCommandResult(
        argv=argv,
        exit_code=exit_code,
        timed_out=timed_out,
        stdout=_captured(stdout, limit),
        stderr=_captured(stderr, limit),
    )


@dataclass
class ScriptedDockerRunner:
    """Model Docker lifecycle responses while recording only application argv."""

    pip_exit_code: int = 0
    pip_exit_codes: tuple[int, ...] | None = None
    pytest_exit_codes: tuple[int, ...] = (0,)
    pytest_timeout: bool = False
    fail_operation: str | None = None
    cleanup_failure: bool = False
    image_present: bool = True
    disconnect_succeeds: bool = True

    def __post_init__(self) -> None:
        self.calls: list[tuple[str, ...]] = []
        self._pytest_calls = 0
        self._pip_calls = 0
        self._create_calls = 0

    def run(
        self,
        argv: tuple[str, ...],
        *,
        timeout_seconds: float,
        output_limit_bytes: int,
        termination_grace_seconds: float,
    ) -> DockerCommandResult:
        del timeout_seconds, termination_grace_seconds
        self.calls.append(argv)
        operation = _operation(argv)
        if operation == self.fail_operation:
            raise ValidationExecutionInfrastructureError(
                ValidationExecutionInfrastructureCode.BACKEND_UNAVAILABLE,
                f"Controlled {operation} infrastructure failure.",
            )
        if operation == "version":
            return _command_result(argv, stdout="26.1.0\n")
        if operation == "image-inspect":
            if not self.image_present:
                return _command_result(argv, exit_code=1, stderr="not found\n")
            return _command_result(argv, stdout=f"{_IMAGE_ID}\n")
        if operation == "pull":
            self.image_present = True
            return _command_result(argv, stdout="pulled\n")
        if operation == "create":
            self._create_calls += 1
            container_id = f"{self._create_calls:064x}"
            return _command_result(argv, stdout=f"{container_id}\n")
        if operation in {"start", "cp"}:
            return _command_result(argv)
        if operation == "pip":
            configured = (
                self.pip_exit_codes[
                    min(self._pip_calls, len(self.pip_exit_codes) - 1)
                ]
                if self.pip_exit_codes
                else self.pip_exit_code
            )
            self._pip_calls += 1
            return _command_result(
                argv,
                exit_code=configured,
                stderr=("dependency resolution failed\n" if configured else ""),
                limit=output_limit_bytes,
            )
        if operation == "disconnect":
            return _command_result(
                argv,
                exit_code=0 if self.disconnect_succeeds else 1,
                stderr="" if self.disconnect_succeeds else "unsupported\n",
            )
        if operation == "pytest":
            configured = self.pytest_exit_codes[
                min(self._pytest_calls, len(self.pytest_exit_codes) - 1)
            ]
            self._pytest_calls += 1
            if self.pytest_timeout:
                return _command_result(
                    argv,
                    exit_code=-15,
                    stdout="partial test output\n",
                    timed_out=True,
                )
            return _command_result(
                argv,
                exit_code=configured,
                stdout="1 passed\n" if configured == 0 else "1 failed\n",
                limit=output_limit_bytes,
            )
        if operation == "rm":
            return _command_result(
                argv,
                exit_code=1 if self.cleanup_failure else 0,
                stderr="remove failed\n" if self.cleanup_failure else "",
            )
        if operation == "container-inspect":
            return _command_result(argv, exit_code=1, stderr="No such object\n")
        raise AssertionError(f"Unexpected Docker command: {argv!r}")


def _operation(argv: tuple[str, ...]) -> str:
    arguments = argv[1:]
    if arguments[:1] == ("version",):
        return "version"
    if arguments[:2] == ("image", "inspect"):
        return "image-inspect"
    if arguments[:1] == ("pull",):
        return "pull"
    if arguments[:1] == ("create",):
        return "create"
    if arguments[:1] == ("start",):
        return "start"
    if arguments[:1] == ("cp",):
        return "cp"
    if arguments[:2] == ("network", "disconnect"):
        return "disconnect"
    if arguments[:1] == ("rm",):
        return "rm"
    if arguments[:2] == ("container", "inspect"):
        return "container-inspect"
    if arguments[:1] == ("exec",):
        return "pip" if "pip" in arguments else "pytest"
    return "unknown"


def _workspace(tmp_path: Path, files: dict[str, str]) -> IsolatedWorkspace:
    workspace = create_isolated_workspace(
        "DOCKER-PYTEST-STAGED-WORKSPACE", parent_directory=tmp_path
    )
    for path, contents in files.items():
        target = workspace.root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(contents)
    return workspace


def _request(workspace: IsolatedWorkspace) -> ValidationExecutionRequest:
    snapshot = snapshot_isolated_workspace(workspace)
    manifest = build_python_dependency_manifest(
        workspace, staged_snapshot_id=snapshot.snapshot_id
    )
    return ValidationExecutionRequest(
        run_id="RUN-DOCKER-PYTEST",
        graph_id="GRAPH-DOCKER-PYTEST-V001",
        graph_version=1,
        task_id="TASK-001",
        request_id="REQUEST-DOCKER-PYTEST",
        attempt_id="ATTEMPT-DOCKER-PYTEST",
        attempt_number=1,
        requirement=TaskValidationRequirement(
            requirement_id="TASK-001-VALIDATION-001",
            profile=ValidationExecutionProfile.PYTHON_PYTEST,
        ),
        source_workspace_id="AUTHORITATIVE-WORKSPACE",
        source_snapshot_id="AUTHORITATIVE-SNAPSHOT",
        staged_workspace_id=workspace.workspace_id,
        staged_snapshot_id=snapshot.snapshot_id,
        dependency_manifest=manifest,
    )


def _executor(runner: ScriptedDockerRunner) -> DockerPytestValidationExecutor:
    return DockerPytestValidationExecutor(
        runner=runner,
        docker_executable="/application/docker",
    )


def test_dependency_manifest_defaults_to_empty_without_pyproject(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path, {"tests/test_example.py": "def test_ok(): pass\n"})
    snapshot = snapshot_isolated_workspace(workspace)

    manifest = build_python_dependency_manifest(
        workspace, staged_snapshot_id=snapshot.snapshot_id
    )

    assert manifest.source_present is False
    assert manifest.source_sha256 == hashlib.sha256(b"").hexdigest()
    assert manifest.normalized_dependencies == ()


def test_dependency_manifest_normalizes_ordinary_requirements(tmp_path: Path) -> None:
    workspace = _workspace(
        tmp_path,
        {
            "pyproject.toml": (
                "[project]\nname='demo'\nversion='0.1.0'\n"
                "dependencies=['requests>=2.32,<3', 'attrs==25.3.0']\n"
            )
        },
    )
    snapshot = snapshot_isolated_workspace(workspace)

    manifest = build_python_dependency_manifest(
        workspace, staged_snapshot_id=snapshot.snapshot_id
    )

    assert manifest.normalized_dependencies == (
        "attrs==25.3.0",
        "requests>=2.32,<3",
    )
    assert manifest.source_sha256 == hashlib.sha256(
        (workspace.root / "pyproject.toml").read_bytes()
    ).hexdigest()


@mark.parametrize(
    "requirement",
    (
        "demo @ https://example.invalid/demo.whl",
        "git+https://example.invalid/demo.git",
        "../local-package",
        "-e ./local-package",
        "--extra-index-url https://example.invalid/simple",
    ),
)
def test_dependency_manifest_rejects_out_of_scope_authority(
    tmp_path: Path, requirement: str
) -> None:
    workspace = _workspace(
        tmp_path,
        {
            "pyproject.toml": (
                "[project]\nname='demo'\nversion='0.1.0'\n"
                f"dependencies=[{requirement!r}]\n"
            )
        },
    )
    snapshot = snapshot_isolated_workspace(workspace)

    with raises(ValidationDependencyManifestError):
        build_python_dependency_manifest(
            workspace, staged_snapshot_id=snapshot.snapshot_id
        )


def test_dependency_manifest_rejects_malformed_pyproject(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path, {"pyproject.toml": "[project\ninvalid"})
    snapshot = snapshot_isolated_workspace(workspace)

    with raises(ValidationDependencyManifestError):
        build_python_dependency_manifest(
            workspace, staged_snapshot_id=snapshot.snapshot_id
        )


def test_pytest_policy_is_fixed_container_authority() -> None:
    policy = python_pytest_validation_policy()

    assert policy.profile is ValidationExecutionProfile.PYTHON_PYTEST
    assert policy.environment_kind is ValidationExecutionEnvironmentKind.DOCKER_DISPOSABLE
    assert policy.dependency_provisioning is ValidationDependencyProvisioning.PIP
    assert policy.container_image_reference == "python:3.12-slim"
    assert policy.argv == ("python", "-m", "pytest", "-q", "tests")
    assert policy.policy_version == "python-pytest-docker-v2"
    assert "--user" in policy.provisioning_argv_prefix
    assert policy.provisioning_argv_prefix[-1] == "pytest"
    assert PUBLIC_PYPI_INDEX_URL in policy.provisioning_argv_prefix


def test_docker_pytest_pass_produces_matching_provisioning_and_execution_evidence(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    workspace = _workspace(
        tmp_path,
        {
            "pyproject.toml": "[project]\nname='demo'\nversion='0.1.0'\ndependencies=['attrs>=25']\n",
            "src/demo.py": "VALUE = 1\n",
            "tests/test_demo.py": "def test_demo(): assert True\n",
        },
    )
    request = _request(workspace)
    runner = ScriptedDockerRunner()
    policy = python_pytest_validation_policy()
    monkeypatch.setattr("agentic_sdlc.docker_validation.os.getuid", lambda: 1201)
    monkeypatch.setattr("agentic_sdlc.docker_validation.os.getgid", lambda: 1202)

    report = _executor(runner).execute(request, policy, workspace)

    assert isinstance(report, GovernedValidationExecutionReport)
    assert len(report.provisioning_evidence) == 1
    provisioning = report.provisioning_evidence[0]
    execution = report.execution_evidence
    assert provisioning.passed is True
    assert provisioning.argv == (
        *policy.provisioning_argv_prefix,
        "attrs>=25",
    )
    assert provisioning.container_cleanup_succeeded is True
    assert execution is not None and execution.passed is True
    assert execution.provisioning_evidence_ids == (provisioning.evidence_id,)
    assert execution.container_image_reference == "python:3.12-slim"
    assert execution.container_image_id == provisioning.container_image_id
    assert execution.container_id == provisioning.container_id
    assert execution.external_network_disconnected is True
    assert execution.container_cleanup_succeeded is True
    assert not validation_provisioning_evidence_errors(request, policy, provisioning)
    assert not validation_execution_evidence_errors(
        request, policy, execution, report.provisioning_evidence
    )
    pip_call = next(call for call in runner.calls if _operation(call) == "pip")
    pytest_call = next(call for call in runner.calls if _operation(call) == "pytest")
    create_call = next(call for call in runner.calls if _operation(call) == "create")
    copy_call = next(call for call in runner.calls if _operation(call) == "cp")
    assert pip_call[-len(provisioning.argv) :] == provisioning.argv
    assert pytest_call[-5:] == policy.argv
    assert create_call[create_call.index("--user") + 1] == "1201:1202"
    assert "--workdir" not in create_call
    assert copy_call == (
        "/application/docker",
        "cp",
        "--archive",
        str(workspace.root),
        f"{provisioning.container_id}:/work",
    )
    assert "PIP_CONFIG_FILE=/dev/null" in pip_call
    assert "PYTEST_DISABLE_PLUGIN_AUTOLOAD=1" in pytest_call
    assert "PYTHONPATH=/work/src" in pytest_call
    assert all("--privileged" not in call for call in runner.calls)
    assert all("/var/run/docker.sock" not in call for call in runner.calls)


def test_missing_numeric_application_identity_fails_before_container_creation(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    workspace = _workspace(tmp_path, {"tests/test_demo.py": "def test_ok(): pass\n"})
    runner = ScriptedDockerRunner()
    monkeypatch.delattr("agentic_sdlc.docker_validation.os.getuid")

    with raises(ValidationExecutionInfrastructureError) as captured:
        _executor(runner).execute(
            _request(workspace), python_pytest_validation_policy(), workspace
        )

    assert captured.value.code is ValidationExecutionInfrastructureCode.POLICY_UNAVAILABLE
    assert not any(_operation(call) == "create" for call in runner.calls)


def test_absent_image_is_pulled_only_from_fixed_policy(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path, {"tests/test_demo.py": "def test_ok(): pass\n"})
    runner = ScriptedDockerRunner(image_present=False)

    report = _executor(runner).execute(
        _request(workspace), python_pytest_validation_policy(), workspace
    )

    assert report.provisioning_evidence[0].image_pulled is True
    pulls = [call for call in runner.calls if _operation(call) == "pull"]
    assert pulls == [("/application/docker", "pull", "python:3.12-slim")]


def test_nonzero_pip_retains_failed_evidence_and_never_runs_pytest(
    tmp_path: Path,
) -> None:
    workspace = _workspace(tmp_path, {"tests/test_demo.py": "def test_ok(): pass\n"})
    runner = ScriptedDockerRunner(pip_exit_code=1)

    report = _executor(runner).execute(
        _request(workspace), python_pytest_validation_policy(), workspace
    )

    assert report.execution_evidence is None
    assert report.provisioning_evidence[0].outcome is ValidationExecutionOutcome.FAILED
    assert report.provisioning_evidence[0].passed is False
    assert report.provisioning_evidence[0].container_cleanup_succeeded is True
    assert not any(_operation(call) == "pytest" for call in runner.calls)


def test_nonzero_and_timed_out_pytest_are_repairable_evidence(
    tmp_path: Path,
) -> None:
    workspace = _workspace(tmp_path, {"tests/test_demo.py": "def test_bad(): assert False\n"})
    failed = _executor(ScriptedDockerRunner(pytest_exit_codes=(1,))).execute(
        _request(workspace), python_pytest_validation_policy(), workspace
    )
    timed_out = _executor(ScriptedDockerRunner(pytest_timeout=True)).execute(
        _request(workspace), python_pytest_validation_policy(), workspace
    )

    assert failed.execution_evidence is not None
    assert failed.execution_evidence.outcome is ValidationExecutionOutcome.FAILED
    assert timed_out.execution_evidence is not None
    assert timed_out.execution_evidence.outcome is ValidationExecutionOutcome.TIMED_OUT
    assert timed_out.execution_evidence.container_cleanup_succeeded is True


@mark.parametrize("operation", ("version", "pull", "create", "start", "cp"))
def test_docker_lifecycle_infrastructure_failures_fail_closed(
    tmp_path: Path, operation: str
) -> None:
    workspace = _workspace(tmp_path, {"tests/test_demo.py": "def test_ok(): pass\n"})
    runner = ScriptedDockerRunner(
        image_present=operation != "pull",
        fail_operation=operation,
    )

    with raises(ValidationExecutionInfrastructureError):
        _executor(runner).execute(
            _request(workspace), python_pytest_validation_policy(), workspace
        )


def test_container_cleanup_failure_blocks_all_evidence_and_success(
    tmp_path: Path,
) -> None:
    workspace = _workspace(tmp_path, {"tests/test_demo.py": "def test_ok(): pass\n"})

    with raises(ValidationExecutionInfrastructureError) as captured:
        _executor(ScriptedDockerRunner(cleanup_failure=True)).execute(
            _request(workspace), python_pytest_validation_policy(), workspace
        )

    assert captured.value.code is ValidationExecutionInfrastructureCode.CLEANUP


def test_network_disconnect_failure_is_recorded_without_false_isolation_claim(
    tmp_path: Path,
) -> None:
    workspace = _workspace(tmp_path, {"tests/test_demo.py": "def test_ok(): pass\n"})
    report = _executor(
        ScriptedDockerRunner(disconnect_succeeds=False)
    ).execute(_request(workspace), python_pytest_validation_policy(), workspace)

    assert report.execution_evidence is not None
    assert report.execution_evidence.external_network_disconnected is False
    assert report.execution_evidence.network_access_allowed is True


def test_docker_cli_runner_uses_argv_shell_false_and_minimal_environment(
    monkeypatch: MonkeyPatch,
) -> None:
    observed: dict[str, object] = {}

    class Process:
        pid = 7171
        returncode = 0
        stdout = BytesIO(b"ok\n")
        stderr = BytesIO(b"")

        def wait(self, timeout: float) -> int:
            del timeout
            return 0

    def fake_popen(*args: object, **kwargs: object) -> Process:
        observed["args"] = args
        observed["kwargs"] = kwargs
        return Process()

    monkeypatch.setattr("agentic_sdlc.docker_validation.subprocess.Popen", fake_popen)

    result = DockerCliCommandRunner().run(
        ("/application/docker", "version"),
        timeout_seconds=1.0,
        output_limit_bytes=1024,
        termination_grace_seconds=1.0,
    )

    kwargs = observed["kwargs"]
    assert isinstance(kwargs, dict)
    assert kwargs["shell"] is False
    assert kwargs["stdin"] is subprocess.DEVNULL
    assert kwargs["start_new_session"] is True
    environment = kwargs["env"]
    assert isinstance(environment, dict)
    assert set(environment) == {"DOCKER_CONFIG", "HOME", "LANG", "LC_ALL", "PATH"}
    assert "OPENAI_API_KEY" not in environment
    assert result.exit_code == 0


def test_docker_cli_runner_process_start_failure_is_infrastructure(
    monkeypatch: MonkeyPatch,
) -> None:
    def fail_start(*args: object, **kwargs: object) -> None:
        del args, kwargs
        raise OSError("Controlled Docker process-start failure.")

    monkeypatch.setattr("agentic_sdlc.docker_validation.subprocess.Popen", fail_start)

    with raises(ValidationExecutionInfrastructureError) as captured:
        DockerCliCommandRunner().run(
            ("/application/docker", "version"),
            timeout_seconds=1.0,
            output_limit_bytes=1024,
            termination_grace_seconds=1.0,
        )

    assert captured.value.code is ValidationExecutionInfrastructureCode.PROCESS_START


def test_docker_cli_runner_bounds_and_hashes_complete_output(
    monkeypatch: MonkeyPatch,
) -> None:
    payload = b"x" * 4096

    class Process:
        pid = 7272
        returncode = 1
        stdout = BytesIO(payload)
        stderr = BytesIO(b"")

        def wait(self, timeout: float) -> int:
            del timeout
            return 1

    monkeypatch.setattr(
        "agentic_sdlc.docker_validation.subprocess.Popen",
        lambda *args, **kwargs: Process(),
    )

    result = DockerCliCommandRunner().run(
        ("/application/docker", "version"),
        timeout_seconds=1.0,
        output_limit_bytes=128,
        termination_grace_seconds=1.0,
    )

    assert result.stdout.total_bytes == len(payload)
    assert len(result.stdout.retained_text.encode()) == 128
    assert result.stdout.sha256 == hashlib.sha256(payload).hexdigest()
    assert result.stdout.truncated is True


def test_pytest_evidence_rejects_stale_provisioning_or_container_identity(
    tmp_path: Path,
) -> None:
    workspace = _workspace(tmp_path, {"tests/test_demo.py": "def test_ok(): pass\n"})
    request = _request(workspace)
    policy = python_pytest_validation_policy()
    report = _executor(ScriptedDockerRunner()).execute(request, policy, workspace)
    execution = report.execution_evidence
    assert execution is not None

    stale_provisioning = report.provisioning_evidence[0].model_copy(
        update={"attempt_id": "STALE-ATTEMPT"}
    )
    assert "provisioning_evidence" in validation_execution_evidence_errors(
        request, policy, execution, (stale_provisioning,)
    )
    stale_container = execution.model_copy(update={"container_id": "c" * 64})
    assert "container_id" in validation_execution_evidence_errors(
        request, policy, stale_container, report.provisioning_evidence
    )


def test_manifest_request_cannot_be_rebound_to_another_staged_snapshot(
    tmp_path: Path,
) -> None:
    workspace = _workspace(tmp_path, {"tests/test_demo.py": "def test_ok(): pass\n"})
    request = _request(workspace)
    manifest = request.dependency_manifest
    assert isinstance(manifest, PythonDependencyManifest)
    altered = request.model_copy(update={"staged_snapshot_id": "STALE-SNAPSHOT"})

    with raises(ValidationExecutionInfrastructureError):
        _executor(ScriptedDockerRunner()).execute(
            altered, python_pytest_validation_policy(), workspace
        )
