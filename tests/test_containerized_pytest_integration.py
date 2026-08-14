"""Opt-in real Docker smoke tests for the fixed PYTHON_PYTEST profile."""

from __future__ import annotations

import os
import stat
from pathlib import Path

from pytest import mark

from agentic_sdlc.docker_validation import DockerPytestValidationExecutor
from agentic_sdlc.validation_execution_contracts import (
    ValidationExecutionOutcome,
    python_pytest_validation_policy,
)
from agentic_sdlc.workspace_mutation import (
    WorkspaceMutationStatus,
    apply_workspace_change_set,
)
from tests.test_containerized_pytest_validation import _request, _workspace
from tests.test_workspace_mutation import _artifact, _change_set


pytestmark = mark.skipif(
    os.environ.get("AGENTIC_SDLC_RUN_DOCKER_TESTS") != "1",
    reason="real Docker validation is an explicit opt-in integration check",
)


def test_real_docker_dependency_free_project_runs_generated_pytest(
    tmp_path: Path,
) -> None:
    workspace = _workspace(
        tmp_path,
        {
            "pyproject.toml": "[project]\nname='demo'\nversion='0.1.0'\ndependencies=[]\n",
            "src/demo.py": "def answer(): return 42\n",
            "tests/test_demo.py": (
                "from demo import answer\n\n"
                "def test_answer():\n    assert answer() == 42\n"
            ),
        },
    )

    report = DockerPytestValidationExecutor().execute(
        _request(workspace), python_pytest_validation_policy(), workspace
    )

    assert report.provisioning_evidence[0].passed is True
    assert report.execution_evidence is not None
    assert report.execution_evidence.passed is True
    assert "1 passed" in report.execution_evidence.retained_stdout
    assert not (workspace.root / "__pycache__").exists()


def test_real_docker_failing_pytest_returns_fail_without_host_side_effect(
    tmp_path: Path,
) -> None:
    host_marker = tmp_path / "container-created-marker"
    workspace = _workspace(
        tmp_path,
        {
            "tests/test_failure.py": (
                "from pathlib import Path\n\n"
                "def test_failure():\n"
                "    Path('/work/container-created-marker').write_text('container')\n"
                "    assert False\n"
            )
        },
    )

    report = DockerPytestValidationExecutor().execute(
        _request(workspace), python_pytest_validation_policy(), workspace
    )

    assert report.execution_evidence is not None
    assert report.execution_evidence.outcome is ValidationExecutionOutcome.FAILED
    assert report.execution_evidence.passed is False
    assert not host_marker.exists()
    assert not (workspace.root / "container-created-marker").exists()


def test_real_docker_governed_third_party_dependency_is_provisioned(
    tmp_path: Path,
) -> None:
    workspace = _workspace(
        tmp_path,
        {
            "pyproject.toml": (
                "[project]\nname='dependency-demo'\nversion='0.1.0'\n"
                "dependencies=['idna==3.10']\n"
            ),
            "tests/test_dependency.py": (
                "import idna\n\n"
                "def test_idna():\n"
                "    assert idna.encode('example.com') == b'example.com'\n"
            ),
        },
    )

    report = DockerPytestValidationExecutor().execute(
        _request(workspace), python_pytest_validation_policy(), workspace
    )

    provisioning = report.provisioning_evidence[0]
    assert provisioning.normalized_dependencies == ("idna==3.10",)
    assert provisioning.passed is True
    assert report.execution_evidence is not None
    assert report.execution_evidence.passed is True


def test_real_docker_reads_restrictive_governed_workspace_without_chmod(
    tmp_path: Path,
) -> None:
    workspace = _workspace(tmp_path, {})
    artifacts = (
        _artifact("pytest.ini", "[pytest]\npythonpath = src\n", index=1),
        _artifact("src/demo.py", "def answer(): return 42\n", index=2),
        _artifact(
            "tests/test_demo.py",
            "from demo import answer\n\ndef test_answer(): assert answer() == 42\n",
            index=3,
        ),
    )
    _, change_set, validation = _change_set(workspace, *artifacts)
    mutation = apply_workspace_change_set(workspace, change_set, validation)
    assert mutation.status is WorkspaceMutationStatus.APPLIED
    governed_modes = {
        relative: stat.S_IMODE((workspace.root / relative).stat().st_mode)
        for relative in (
            "pytest.ini",
            "src",
            "src/demo.py",
            "tests",
            "tests/test_demo.py",
        )
    }
    assert governed_modes == {
        "pytest.ini": 0o600,
        "src": 0o700,
        "src/demo.py": 0o600,
        "tests": 0o700,
        "tests/test_demo.py": 0o600,
    }

    report = DockerPytestValidationExecutor().execute(
        _request(workspace), python_pytest_validation_policy(), workspace
    )

    assert report.provisioning_evidence[0].passed is True
    assert report.execution_evidence is not None
    assert report.execution_evidence.passed is True
    assert "1 passed" in report.execution_evidence.retained_stdout
    assert {
        relative: stat.S_IMODE((workspace.root / relative).stat().st_mode)
        for relative in governed_modes
    } == governed_modes
