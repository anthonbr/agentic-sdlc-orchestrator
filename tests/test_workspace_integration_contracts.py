"""Tests for pure governed task-to-workspace integration vocabulary."""

from __future__ import annotations

from pydantic import ValidationError
from pytest import mark, raises

from agentic_sdlc.task_execution_contracts import (
    EngineeringArtifact,
    EngineeringArtifactType,
)
from agentic_sdlc.workspace_contracts import workspace_file_content_hash
from agentic_sdlc.workspace_integration_contracts import (
    ArtifactMaterializationIntent,
    RepositoryContext,
    RepositoryPathObservation,
    TaskAttemptExitDecision,
    TaskAttemptExitDisposition,
    TaskMaterializationPolicy,
    WorkspaceBinding,
    WorkspaceIntegrityStatus,
    WorkspaceIntegrationContractError,
    build_repository_context,
    repository_context_identity_is_valid,
)


def _artifact(
    artifact_type: EngineeringArtifactType = EngineeringArtifactType.DESIGN,
) -> EngineeringArtifact:
    return EngineeringArtifact(
        artifact_id="ARTIFACT-001",
        lineage_id="LINEAGE-001",
        artifact_type=artifact_type,
        logical_name="service design",
        content="complete artifact contents\n",
        content_hash="0" * 64,
        output_index=1,
        requirement_spec_id="SPEC-001",
        graph_id="GRAPH-001",
        task_id="TASK-001",
        request_id="REQUEST-001",
        attempt_id="ATTEMPT-001",
        attempt_number=1,
        requirement_refs=("FR-001",),
        acceptance_criteria_refs=("AC-001",),
        risk_refs=(),
        ambiguity_refs=(),
        created_at="2026-08-10T12:00:00+00:00",
    )


def _binding() -> WorkspaceBinding:
    return WorkspaceBinding(
        workspace_id="WORKSPACE-001",
        snapshot_id="WORKSPACE-SNAPSHOT-001",
    )


def _existing(path: str, content: str) -> RepositoryPathObservation:
    return RepositoryPathObservation(
        path=path,
        exists=True,
        content=content,
        content_hash=workspace_file_content_hash(content),
    )


def test_workspace_binding_is_exact_minimal_and_immutable() -> None:
    binding = _binding()

    assert binding.workspace_id == "WORKSPACE-001"
    assert binding.snapshot_id == "WORKSPACE-SNAPSHOT-001"
    assert set(WorkspaceBinding.model_fields) == {"workspace_id", "snapshot_id"}
    with raises(ValidationError):
        binding.snapshot_id = "OTHER"  # type: ignore[misc]


@mark.parametrize("field", ("workspace_id", "snapshot_id"))
def test_workspace_binding_rejects_blank_identity(field: str) -> None:
    values = {
        "workspace_id": "WORKSPACE-001",
        "snapshot_id": "WORKSPACE-SNAPSHOT-001",
    }
    values[field] = ""

    with raises(ValidationError):
        WorkspaceBinding(**values)


def test_existing_repository_observation_binds_complete_content_and_hash() -> None:
    observation = _existing("src/service.py", "def service():\n    pass\n")

    assert observation.exists is True
    assert observation.path == "src/service.py"
    assert observation.content_hash == workspace_file_content_hash(
        observation.content or ""
    )
    with raises(ValidationError):
        observation.content = "changed"  # type: ignore[misc]


def test_nonexistent_repository_observation_carries_no_content_state() -> None:
    observation = RepositoryPathObservation(
        path="tests/test_service.py",
        exists=False,
        content=None,
        content_hash=None,
    )

    assert observation.model_dump(mode="json") == {
        "path": "tests/test_service.py",
        "exists": False,
        "content": None,
        "content_hash": None,
    }


@mark.parametrize(
    "values",
    (
        {"exists": False, "content": "ghost", "content_hash": None},
        {"exists": False, "content": None, "content_hash": "0" * 64},
        {"exists": True, "content": None, "content_hash": "0" * 64},
        {"exists": True, "content": "present", "content_hash": None},
        {"exists": True, "content": "present", "content_hash": "0" * 64},
    ),
)
def test_repository_observation_rejects_contradictory_state(
    values: dict[str, object],
) -> None:
    with raises(ValidationError):
        RepositoryPathObservation(path="src/service.py", **values)


@mark.parametrize(
    "path",
    ("", "../outside.py", "/absolute.py", ".git/config", "src\\file.py"),
)
def test_repository_observation_reuses_conservative_path_policy(path: str) -> None:
    with raises(ValidationError):
        RepositoryPathObservation(
            path=path,
            exists=False,
            content=None,
            content_hash=None,
        )


def test_repository_context_identity_order_and_json_are_deterministic() -> None:
    first = _existing("src/a.py", "a\n")
    second = RepositoryPathObservation(
        path="src/b.py",
        exists=False,
        content=None,
        content_hash=None,
    )

    forward = build_repository_context(_binding(), (first, second))
    reverse = build_repository_context(_binding(), (second, first))
    restored = RepositoryContext.model_validate_json(forward.model_dump_json())

    assert forward == reverse == restored
    assert forward.repository_context_id.startswith("REPOSITORY-CONTEXT-")
    assert tuple(item.path for item in forward.observations) == (
        "src/a.py",
        "src/b.py",
    )
    assert repository_context_identity_is_valid(forward) is True
    with raises(ValidationError):
        forward.observations = ()  # type: ignore[misc]


def test_repository_context_rejects_duplicate_paths_and_stale_identity() -> None:
    observation = _existing("src/service.py", "before\n")
    with raises(WorkspaceIntegrationContractError, match="unique"):
        build_repository_context(_binding(), (observation, observation))

    context = build_repository_context(_binding(), (observation,))
    tampered = context.model_copy(
        update={
            "observations": (_existing("src/service.py", "after\n"),),
        }
    )
    assert repository_context_identity_is_valid(tampered) is False


def test_materialization_intent_is_artifact_type_independent_proposal() -> None:
    semantic_artifact = _artifact(EngineeringArtifactType.DESIGN)
    intent = ArtifactMaterializationIntent(
        artifact_id=semantic_artifact.artifact_id,
        target_path="src/service.py",
    )

    assert semantic_artifact.artifact_type is EngineeringArtifactType.DESIGN
    assert intent.artifact_id == semantic_artifact.artifact_id
    assert set(ArtifactMaterializationIntent.model_fields) == {
        "artifact_id",
        "target_path",
    }
    assert "operation" not in intent.model_dump()
    assert ArtifactMaterializationIntent.model_validate_json(
        intent.model_dump_json()
    ) == intent
    with raises(ValidationError):
        intent.target_path = "other.py"  # type: ignore[misc]


@mark.parametrize("path", ("../outside.py", ".env", "C:\\outside.py"))
def test_materialization_intent_rejects_invalid_target(path: str) -> None:
    with raises(ValidationError):
        ArtifactMaterializationIntent(
            artifact_id="ARTIFACT-001",
            target_path=path,
        )


def test_task_materialization_policy_has_exact_values() -> None:
    assert tuple(item.value for item in TaskMaterializationPolicy) == (
        "FORBIDDEN",
        "ALLOWED",
        "REQUIRED",
    )


def test_task_attempt_exit_disposition_has_exact_values() -> None:
    assert tuple(item.value for item in TaskAttemptExitDisposition) == (
        "SUCCEED_TASK",
        "RETRY_TASK",
        "FAIL_TASK",
        "SAFE_STOP_RUN",
    )


def test_task_attempt_exit_decision_is_immutable_canonical_and_round_trips() -> None:
    decision = TaskAttemptExitDecision(
        task_id="TASK-001",
        attempt_number=1,
        request_id="REQUEST-001",
        attempt_id="ATTEMPT-001",
        disposition=TaskAttemptExitDisposition.SUCCEED_TASK,
        reason_code="VERIFIED_POSTCONDITIONS",
        evidence_ids=("ARTIFACT-001", "VALIDATION-001"),
    )

    assert TaskAttemptExitDecision.model_validate_json(
        decision.model_dump_json()
    ) == decision
    with raises(ValidationError):
        decision.disposition = (  # type: ignore[misc]
            TaskAttemptExitDisposition.FAIL_TASK
        )
    with raises(ValidationError):
        TaskAttemptExitDecision(
            **decision.model_dump(exclude={"evidence_ids"}),
            evidence_ids=("VALIDATION-001", "ARTIFACT-001"),
        )


def test_workspace_integrity_status_has_exact_values() -> None:
    assert tuple(item.value for item in WorkspaceIntegrityStatus) == (
        "VERIFIED",
        "UNPROVABLE",
    )
