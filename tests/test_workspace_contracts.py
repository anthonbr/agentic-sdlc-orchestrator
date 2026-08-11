"""Tests for pure workspace snapshots, change sets, and conflict analysis."""

from __future__ import annotations

from hashlib import sha256

from pydantic import ValidationError
from pytest import mark, raises

from agentic_sdlc.project_delivery import ProjectDeliverableRole
from agentic_sdlc.task_execution_contracts import (
    ArtifactMaterializationProposal,
    ArtifactOutput,
    EngineeringArtifact,
    EngineeringArtifactType,
    TaskExecutionResult,
    TaskExecutionValidationResult,
)
from agentic_sdlc.task_graph import Task, TaskMaterializationPolicy, TaskType
from agentic_sdlc.workspace_contracts import (
    ArtifactMaterializationIntent,
    ArtifactMaterializationIssueCode,
    ArtifactMaterializationValidationResult,
    WorkspaceChangeOperation,
    WorkspaceChangeSet,
    WorkspaceChangeSetIssueCode,
    WorkspaceChangeSetValidationResult,
    WorkspaceContractError,
    WorkspaceFileState,
    WorkspaceSnapshot,
    analyze_workspace_change_set_conflicts,
    build_workspace_change_set,
    build_workspace_snapshot,
    canonicalize_artifact_materialization_proposals,
    normalize_repository_path,
    validate_artifact_materialization,
    validate_workspace_change_set,
    validate_workspace_change_set_preimages,
    workspace_change_set_identity_is_valid,
    workspace_file_content_hash,
)


def _result_with_proposal(
    artifact: EngineeringArtifact,
    *,
    output_index: int = 1,
    target_path: str = "src/target.py",
) -> TaskExecutionResult:
    return TaskExecutionResult(
        request_id=artifact.request_id,
        attempt_id=artifact.attempt_id,
        task_id=artifact.task_id,
        summary="Proposed semantic output and desired repository path.",
        outputs=(
            ArtifactOutput(
                artifact_type=artifact.artifact_type,
                logical_name=artifact.logical_name,
                content=artifact.content,
            ),
        ),
        materialization_proposals=(
            ArtifactMaterializationProposal(
                output_index=output_index,
                target_path=target_path,
            ),
        ),
        assumptions=(),
        risks=(),
    )


def _artifact(
    path: str = "src/url_shortener/service.py",
    content: str = "def shorten(url: str) -> str:\n    return url\n",
    *,
    artifact_id: str = "ARTIFACT-001",
    lineage_id: str = "artifact-lineage-001",
    artifact_type: EngineeringArtifactType = EngineeringArtifactType.SOURCE,
    output_index: int = 1,
    task_id: str = "TASK-001",
    request_id: str = "REQUEST-001",
    attempt_id: str = "ATTEMPT-001",
    attempt_number: int = 1,
) -> EngineeringArtifact:
    return EngineeringArtifact(
        artifact_id=artifact_id,
        lineage_id=lineage_id,
        artifact_type=artifact_type,
        logical_name=path,
        content=content,
        content_hash=sha256(f"canonical:{artifact_id}".encode()).hexdigest(),
        output_index=output_index,
        requirement_spec_id="SPEC-001",
        graph_id="GRAPH-001",
        task_id=task_id,
        request_id=request_id,
        attempt_id=attempt_id,
        attempt_number=attempt_number,
        requirement_refs=("FR-001",),
        acceptance_criteria_refs=("AC-001",),
        risk_refs=(),
        ambiguity_refs=(),
        created_at="2026-08-10T12:00:00+00:00",
    )


def _validation(
    *artifacts: EngineeringArtifact,
    passed: bool = True,
) -> TaskExecutionValidationResult:
    first = artifacts[0]
    return TaskExecutionValidationResult(
        request_id=first.request_id,
        attempt_id=first.attempt_id,
        task_id=first.task_id,
        passed=passed,
        artifact_ids=tuple(artifact.artifact_id for artifact in artifacts),
        checks=(),
        errors=() if passed else ("failed",),
    )


def _state(path: str, content: str) -> WorkspaceFileState:
    return WorkspaceFileState(
        path=path,
        content_hash=workspace_file_content_hash(content),
    )


def _task(
    artifact: EngineeringArtifact,
    policy: TaskMaterializationPolicy = TaskMaterializationPolicy.REQUIRED,
    roles: tuple[ProjectDeliverableRole, ...] = (),
) -> Task:
    return Task(
        task_id=artifact.task_id,
        lineage_id=f"task-lineage-{artifact.task_id}",
        source_key=artifact.task_id.casefold().replace("-", "_"),
        title="Materialize desired repository state",
        description="Propose complete desired repository file contents.",
        task_type=TaskType.IMPLEMENTATION,
        materialization_policy=policy,
        depends_on=(),
        requirement_refs=artifact.requirement_refs,
        acceptance_criteria_refs=artifact.acceptance_criteria_refs,
        risk_refs=artifact.risk_refs,
        ambiguity_refs=artifact.ambiguity_refs,
        expected_outputs=("desired repository state",),
        deliverable_roles=roles,
    )


def _intents(
    artifacts: tuple[EngineeringArtifact, ...],
) -> tuple[ArtifactMaterializationIntent, ...]:
    return tuple(
        ArtifactMaterializationIntent(
            artifact_id=artifact.artifact_id,
            target_path=artifact.logical_name,
        )
        for artifact in artifacts
    )


def _materialization_validation(
    validation: TaskExecutionValidationResult,
    artifacts: tuple[EngineeringArtifact, ...],
    intents: tuple[ArtifactMaterializationIntent, ...] | None = None,
    *,
    policy: TaskMaterializationPolicy = TaskMaterializationPolicy.REQUIRED,
) -> ArtifactMaterializationValidationResult:
    return validate_artifact_materialization(
        _task(artifacts[0], policy),
        validation,
        artifacts,
        _intents(artifacts) if intents is None else intents,
    )


def _build_change_set(
    snapshot: WorkspaceSnapshot,
    validation: TaskExecutionValidationResult,
    artifacts: tuple[EngineeringArtifact, ...],
    intents: tuple[ArtifactMaterializationIntent, ...] | None = None,
) -> WorkspaceChangeSet:
    materialization = _materialization_validation(
        validation, artifacts, intents
    )
    return build_workspace_change_set(
        snapshot, validation, artifacts, materialization
    )


def _validate_change_set(
    change_set: WorkspaceChangeSet,
    snapshot: WorkspaceSnapshot,
    artifacts: tuple[EngineeringArtifact, ...],
    intents: tuple[ArtifactMaterializationIntent, ...] | None = None,
) -> WorkspaceChangeSetValidationResult:
    materialization = _materialization_validation(
        _validation(*artifacts), artifacts, intents
    )
    return validate_workspace_change_set(
        change_set, snapshot, artifacts, materialization
    )


def _change_set(
    snapshot_files: tuple[WorkspaceFileState, ...] = (),
    *artifacts: EngineeringArtifact,
) -> tuple[WorkspaceChangeSet, WorkspaceSnapshot]:
    selected = artifacts or (_artifact(),)
    snapshot = build_workspace_snapshot("WORKSPACE-001", snapshot_files)
    change_set = _build_change_set(
        snapshot,
        _validation(*selected),
        selected,
    )
    return change_set, snapshot


def _codes(
    result: WorkspaceChangeSetValidationResult,
) -> set[WorkspaceChangeSetIssueCode]:
    return {issue.code for issue in result.issues}


def test_executor_materialization_proposal_correlates_output_index_to_artifact() -> None:
    artifact = _artifact(
        path="semantic-name", artifact_type=EngineeringArtifactType.TEST
    )
    result = _result_with_proposal(
        artifact,
        target_path="tests/test_api.py",
    )

    intents = canonicalize_artifact_materialization_proposals(result, (artifact,))

    assert intents == (
        ArtifactMaterializationIntent(
            artifact_id=artifact.artifact_id,
            target_path="tests/test_api.py",
        ),
    )
    assert artifact.logical_name == "semantic-name"


def test_materialization_target_does_not_change_semantic_artifact_identity() -> None:
    artifact = _artifact(path="semantic-name")
    first = canonicalize_artifact_materialization_proposals(
        _result_with_proposal(artifact, target_path="src/a.py"),
        (artifact,),
    )
    second = canonicalize_artifact_materialization_proposals(
        _result_with_proposal(artifact, target_path="src/b.py"),
        (artifact,),
    )

    assert first[0].artifact_id == second[0].artifact_id == artifact.artifact_id
    assert first[0].target_path != second[0].target_path


def test_invalid_materialization_output_index_fails_deterministically() -> None:
    artifact = _artifact()

    with raises(WorkspaceContractError, match="unknown output index: 2"):
        canonicalize_artifact_materialization_proposals(
            _result_with_proposal(artifact, output_index=2),
            (artifact,),
        )


def test_invalid_executor_target_path_fails_before_intent_authority() -> None:
    artifact = _artifact()

    with raises(WorkspaceContractError, match="violates repository path policy"):
        canonicalize_artifact_materialization_proposals(
            _result_with_proposal(artifact, target_path="../../escape.py"),
            (artifact,),
        )


@mark.parametrize(
    "target_path",
    (
        "sdlc-artifacts",
        "sdlc-artifacts/foo.json",
        "sdlc-artifacts/nested/file.txt",
    ),
)
def test_sdlc_artifact_namespace_is_reserved_before_intent_authority(
    target_path: str,
) -> None:
    artifact = _artifact()

    with raises(WorkspaceContractError, match="violates repository path policy"):
        canonicalize_artifact_materialization_proposals(
            _result_with_proposal(artifact, target_path=target_path),
            (artifact,),
        )


def _materialization_codes(
    result: ArtifactMaterializationValidationResult,
) -> set[ArtifactMaterializationIssueCode]:
    return {issue.code for issue in result.issues}


def test_snapshot_identity_and_order_are_deterministic() -> None:
    first = _state("src/b.py", "b\n")
    second = _state("src/a.py", "a\n")

    snapshot_a = build_workspace_snapshot("WORKSPACE-001", (first, second))
    snapshot_b = build_workspace_snapshot("WORKSPACE-001", (second, first))

    assert snapshot_a == snapshot_b
    assert tuple(item.path for item in snapshot_a.files) == (
        "src/a.py",
        "src/b.py",
    )
    assert snapshot_a.file_state("src/a.py") == second
    assert snapshot_a.file_state("missing.py") is None


def test_snapshot_content_change_changes_identity() -> None:
    before = build_workspace_snapshot(
        "WORKSPACE-001", (_state("src/a.py", "before\n"),)
    )
    after = build_workspace_snapshot(
        "WORKSPACE-001", (_state("src/a.py", "after\n"),)
    )

    assert before.snapshot_id != after.snapshot_id


def test_snapshot_rejects_duplicate_paths() -> None:
    with raises(WorkspaceContractError, match="unique"):
        build_workspace_snapshot(
            "WORKSPACE-001",
            (_state("src/a.py", "a\n"), _state("src/a.py", "b\n")),
        )


@mark.parametrize(
    "path",
    (
        "",
        "/absolute/path.py",
        "../../.ssh/config",
        "src/../../outside.py",
        "C:\\somewhere\\file.py",
        "C:/somewhere/file.py",
        "src\\ambiguous.py",
        "src/./service.py",
        "src//service.py",
        "src/service.py\x00ignored",
        ".git",
        ".git/config",
        ".env",
        ".venv/bin/python",
        "venv/bin/python",
        "sdlc-artifacts",
        "sdlc-artifacts/foo.json",
        "sdlc-artifacts/nested/file.txt",
    ),
)
def test_repository_path_policy_rejects_dangerous_paths(path: str) -> None:
    with raises(WorkspaceContractError):
        normalize_repository_path(path)


@mark.parametrize(
    "path",
    (
        "src/url_shortener/service.py",
        "tests/test_service.py",
        "README.md",
        ".env.example",
        ".gitignore",
    ),
)
def test_repository_path_policy_allows_safe_paths(path: str) -> None:
    assert normalize_repository_path(path) == path


def test_snapshot_can_describe_protected_state_without_authorizing_mutation() -> None:
    snapshot = build_workspace_snapshot(
        "WORKSPACE-001", (_state(".env", "SECRET=placeholder\n"),)
    )

    assert snapshot.file_state(".env") is not None
    with raises(ValidationError, match="Protected"):
        artifact = _artifact(path=".env")
        _build_change_set(snapshot, _validation(artifact), (artifact,))


def test_materialization_intent_defines_target_and_preserves_provenance() -> None:
    artifact = _artifact(path="semantic implementation output")
    snapshot = build_workspace_snapshot("WORKSPACE-001")
    intent = ArtifactMaterializationIntent(
        artifact_id=artifact.artifact_id,
        target_path="src/url_shortener/service.py",
    )
    change_set = _build_change_set(
        snapshot, _validation(artifact), (artifact,), (intent,)
    )
    change = change_set.file_changes[0]

    assert change.path == intent.target_path
    assert change.path != artifact.logical_name
    assert change.desired_content == artifact.content
    assert change.artifact_id == artifact.artifact_id
    assert change.artifact_lineage_id == artifact.lineage_id
    assert change.desired_content_hash == workspace_file_content_hash(artifact.content)
    assert change_set.materialized_artifact_ids == (artifact.artifact_id,)
    assert "source_artifact_ids" not in change_set.model_dump()


@mark.parametrize(
    ("artifact_type", "target_path"),
    (
        (EngineeringArtifactType.TEST, "tests/test_api.py"),
        (EngineeringArtifactType.DOCUMENTATION, "README.md"),
        (EngineeringArtifactType.SCHEMA, "openapi.yaml"),
    ),
)
def test_semantic_artifact_type_does_not_control_materialization(
    artifact_type: EngineeringArtifactType,
    target_path: str,
) -> None:
    artifact = _artifact(
        path=f"semantic {artifact_type.value.casefold()} output",
        artifact_type=artifact_type,
    )
    snapshot = build_workspace_snapshot("WORKSPACE-001")
    intent = ArtifactMaterializationIntent(
        artifact_id=artifact.artifact_id,
        target_path=target_path,
    )

    change_set = _build_change_set(
        snapshot, _validation(artifact), (artifact,), (intent,)
    )

    assert change_set.file_changes[0].artifact_id == artifact.artifact_id
    assert change_set.file_changes[0].path == target_path


@mark.parametrize(
    ("policy", "has_intent", "expected_passed"),
    (
        (TaskMaterializationPolicy.FORBIDDEN, False, True),
        (TaskMaterializationPolicy.FORBIDDEN, True, False),
        (TaskMaterializationPolicy.ALLOWED, False, True),
        (TaskMaterializationPolicy.ALLOWED, True, True),
        (TaskMaterializationPolicy.REQUIRED, False, False),
        (TaskMaterializationPolicy.REQUIRED, True, True),
    ),
)
def test_materialization_validation_enforces_approved_task_policy(
    policy: TaskMaterializationPolicy,
    has_intent: bool,
    expected_passed: bool,
) -> None:
    artifact = _artifact(artifact_type=EngineeringArtifactType.TEST)
    intents = _intents((artifact,)) if has_intent else ()

    result = _materialization_validation(
        _validation(artifact), (artifact,), intents, policy=policy
    )

    assert result.passed is expected_passed
    assert result.policy is policy
    if not expected_passed:
        assert ArtifactMaterializationIssueCode.POLICY in _materialization_codes(
            result
        )


@mark.parametrize(
    ("role", "artifact_type", "path"),
    (
        (
            ProjectDeliverableRole.RUNNABLE_ENTRYPOINT,
            EngineeringArtifactType.SOURCE,
            "src/product/app.py",
        ),
        (
            ProjectDeliverableRole.AUTOMATED_TESTS,
            EngineeringArtifactType.TEST,
            "tests/test_product.py",
        ),
        (
            ProjectDeliverableRole.RUN_INSTRUCTIONS,
            EngineeringArtifactType.DOCUMENTATION,
            "README.md",
        ),
    ),
)
def test_role_materialization_binds_canonical_artifact_type_and_path(
    role: ProjectDeliverableRole,
    artifact_type: EngineeringArtifactType,
    path: str,
) -> None:
    artifact = _artifact(path=path, artifact_type=artifact_type)

    result = validate_artifact_materialization(
        _task(artifact, roles=(role,)),
        _validation(artifact),
        (artifact,),
        _intents((artifact,)),
    )

    assert result.passed is True


def test_run_instructions_materialization_rejects_nonroot_documentation() -> None:
    artifact = _artifact(
        path="docs/run.md",
        artifact_type=EngineeringArtifactType.DOCUMENTATION,
    )

    result = validate_artifact_materialization(
        _task(
            artifact,
            roles=(ProjectDeliverableRole.RUN_INSTRUCTIONS,),
        ),
        _validation(artifact),
        (artifact,),
        _intents((artifact,)),
    )

    assert result.passed is False
    assert _materialization_codes(result) == {
        ArtifactMaterializationIssueCode.DELIVERABLE_ROLE
    }
    assert "root README.md" in result.issues[0].detail


def test_materialization_rejects_unknown_duplicate_artifact_and_path() -> None:
    first = _artifact(path="semantic first")
    second = _artifact(
        path="semantic second",
        artifact_id="ARTIFACT-002",
        lineage_id="artifact-lineage-002",
        output_index=2,
    )
    intents = (
        ArtifactMaterializationIntent(
            artifact_id=first.artifact_id, target_path="src/shared.py"
        ),
        ArtifactMaterializationIntent(
            artifact_id=first.artifact_id, target_path="tests/shared.py"
        ),
        ArtifactMaterializationIntent(
            artifact_id=second.artifact_id, target_path="src/shared.py"
        ),
        ArtifactMaterializationIntent(
            artifact_id="ARTIFACT-MISSING", target_path="missing.py"
        ),
    )

    result = _materialization_validation(
        _validation(first, second), (first, second), intents
    )

    assert result.passed is False
    assert _materialization_codes(result) == {
        ArtifactMaterializationIssueCode.ARTIFACT_REFERENCE,
        ArtifactMaterializationIssueCode.DUPLICATE_ARTIFACT,
        ArtifactMaterializationIssueCode.DUPLICATE_PATH,
    }


def test_materialization_validation_requires_passed_exact_artifact_evidence() -> None:
    first = _artifact(path="src/first.py")
    second = _artifact(
        path="src/second.py",
        artifact_id="ARTIFACT-002",
        lineage_id="artifact-lineage-002",
        output_index=2,
    )
    validation = _validation(first, second)

    failed = validate_artifact_materialization(
        _task(first),
        validation.model_copy(update={"passed": False, "errors": ("failed",)}),
        (first, second),
        _intents((first, second)),
    )
    reordered = validate_artifact_materialization(
        _task(first),
        validation,
        (second, first),
        _intents((first, second)),
    )

    assert ArtifactMaterializationIssueCode.TASK_VALIDATION in (
        _materialization_codes(failed)
    )
    assert ArtifactMaterializationIssueCode.ARTIFACT_SET in (
        _materialization_codes(reordered)
    )


def test_materialization_validation_rejects_artifact_lineage_mismatch() -> None:
    artifact = _artifact()
    mismatched = artifact.model_copy(update={"task_id": "TASK-OTHER"})

    result = validate_artifact_materialization(
        _task(artifact),
        _validation(artifact),
        (mismatched,),
        _intents((artifact,)),
    )

    assert ArtifactMaterializationIssueCode.LINEAGE in _materialization_codes(result)


def test_source_artifact_without_intent_is_evidence_only_when_allowed() -> None:
    artifact = _artifact(artifact_type=EngineeringArtifactType.SOURCE)
    materialization = _materialization_validation(
        _validation(artifact),
        (artifact,),
        (),
        policy=TaskMaterializationPolicy.ALLOWED,
    )

    assert materialization.passed is True
    assert materialization.intents == ()
    with raises(WorkspaceContractError, match="at least one validated intent"):
        build_workspace_change_set(
            build_workspace_snapshot("WORKSPACE-001"),
            _validation(artifact),
            (artifact,),
            materialization,
        )


def test_change_set_validation_rejects_tampered_materialization_evidence() -> None:
    artifact = _artifact(path="semantic output")
    validation = _validation(artifact)
    intent = ArtifactMaterializationIntent(
        artifact_id=artifact.artifact_id, target_path="src/service.py"
    )
    materialization = _materialization_validation(
        validation, (artifact,), (intent,)
    )
    snapshot = build_workspace_snapshot("WORKSPACE-001")
    change_set = build_workspace_change_set(
        snapshot, validation, (artifact,), materialization
    )
    tampered = materialization.model_copy(
        update={
            "intents": (
                intent.model_copy(update={"target_path": "src/other.py"}),
            )
        }
    )

    result = validate_workspace_change_set(
        change_set, snapshot, (artifact,), tampered
    )

    assert result.passed is False
    assert WorkspaceChangeSetIssueCode.MATERIALIZATION_EVIDENCE in _codes(result)


def test_change_set_requires_passed_exact_artifact_validation() -> None:
    artifact = _artifact()
    snapshot = build_workspace_snapshot("WORKSPACE-001")

    with raises(WorkspaceContractError, match="passed"):
        _build_change_set(
            snapshot, _validation(artifact, passed=False), (artifact,)
        )
    with raises(WorkspaceContractError, match="passed materialization"):
        _build_change_set(
            snapshot,
            _validation(artifact),
            (artifact, _artifact(artifact_id="ARTIFACT-EXTRA", output_index=2)),
        )


def test_operation_derivation_create_modify_and_no_change() -> None:
    create = _artifact(path="src/create.py", content="created\n")
    modify = _artifact(
        path="src/modify.py",
        content="after\n",
        artifact_id="ARTIFACT-002",
        lineage_id="artifact-lineage-002",
        output_index=2,
    )
    unchanged = _artifact(
        path="src/unchanged.py",
        content="same\n",
        artifact_id="ARTIFACT-003",
        lineage_id="artifact-lineage-003",
        output_index=3,
    )
    snapshot = build_workspace_snapshot(
        "WORKSPACE-001",
        (
            _state("src/modify.py", "before\n"),
            _state("src/unchanged.py", "same\n"),
        ),
    )

    change_set = _build_change_set(
        snapshot,
        _validation(create, modify, unchanged),
        (create, modify, unchanged),
    )
    changes = {change.path: change for change in change_set.file_changes}

    assert changes["src/create.py"].operation is WorkspaceChangeOperation.CREATE
    assert changes["src/create.py"].expected_preimage_hash is None
    assert changes["src/modify.py"].operation is WorkspaceChangeOperation.MODIFY
    expected_hash = workspace_file_content_hash("before\n")
    assert changes["src/modify.py"].expected_preimage_hash == expected_hash
    assert (
        changes["src/unchanged.py"].operation
        is WorkspaceChangeOperation.NO_CHANGE
    )
    assert changes[
        "src/unchanged.py"
    ].expected_preimage_hash == workspace_file_content_hash("same\n")


def test_duplicate_source_destination_is_rejected() -> None:
    first = _artifact()
    second = _artifact(
        artifact_id="ARTIFACT-002",
        lineage_id="artifact-lineage-002",
        output_index=2,
    )
    snapshot = build_workspace_snapshot("WORKSPACE-001")

    with raises(WorkspaceContractError, match="passed materialization"):
        _build_change_set(
            snapshot, _validation(first, second), (first, second)
        )


def test_change_set_identity_and_order_ignore_artifact_iterable_order() -> None:
    source_b = _artifact(
        path="src/b.py",
        artifact_id="ARTIFACT-002",
        lineage_id="artifact-lineage-002",
        output_index=2,
    )
    source_a = _artifact(path="src/a.py")
    snapshot = build_workspace_snapshot("WORKSPACE-001")
    validation = _validation(source_b, source_a)

    artifacts = (source_b, source_a)
    intents = _intents(artifacts)
    first = _build_change_set(snapshot, validation, artifacts, intents)
    second = _build_change_set(
        snapshot, validation, artifacts, tuple(reversed(intents))
    )

    assert first == second
    assert tuple(change.path for change in first.file_changes) == (
        "src/a.py",
        "src/b.py",
    )


def test_valid_change_set_passes_deterministically() -> None:
    artifact = _artifact()
    change_set, snapshot = _change_set((), artifact)

    first = _validate_change_set(change_set, snapshot, (artifact,))
    second = _validate_change_set(change_set, snapshot, (artifact,))

    assert first.passed is True
    assert first.issues == ()
    assert first == second
    assert workspace_change_set_identity_is_valid(change_set) is True


def test_change_set_validation_rejects_noncanonical_snapshot_identity() -> None:
    artifact = _artifact()
    change_set, snapshot = _change_set((), artifact)
    tampered_snapshot = snapshot.model_copy(update={"snapshot_id": "TAMPERED"})

    result = _validate_change_set(
        change_set, tampered_snapshot, (artifact,)
    )

    assert result.passed is False
    assert WorkspaceChangeSetIssueCode.SNAPSHOT_ID in _codes(result)


def test_duplicate_artifact_validation_is_independent_of_input_order() -> None:
    artifact = _artifact()
    change_set, snapshot = _change_set((), artifact)
    conflicting_duplicate = artifact.model_copy(
        update={
            "lineage_id": "conflicting-lineage",
            "content": "different proposed contents\n",
        }
    )

    forward = _validate_change_set(
        change_set, snapshot, (artifact, conflicting_duplicate)
    )
    reverse = _validate_change_set(
        change_set, snapshot, (conflicting_duplicate, artifact)
    )

    assert forward == reverse
    assert forward.passed is False
    assert WorkspaceChangeSetIssueCode.ARTIFACT_REFERENCE in _codes(forward)


@mark.parametrize(
    ("tamper", "expected_code"),
    (
        ("workspace", WorkspaceChangeSetIssueCode.WORKSPACE_ID),
        ("snapshot", WorkspaceChangeSetIssueCode.SNAPSHOT_ID),
        ("request", WorkspaceChangeSetIssueCode.LINEAGE),
        ("attempt", WorkspaceChangeSetIssueCode.LINEAGE),
        ("task", WorkspaceChangeSetIssueCode.LINEAGE),
        ("desired_hash", WorkspaceChangeSetIssueCode.DESIRED_CONTENT_HASH),
        ("desired_content", WorkspaceChangeSetIssueCode.DESIRED_CONTENT),
        ("preimage", WorkspaceChangeSetIssueCode.PREIMAGE_HASH),
        ("operation", WorkspaceChangeSetIssueCode.OPERATION),
        ("unsupported_operation", WorkspaceChangeSetIssueCode.OPERATION),
        ("artifact_lineage", WorkspaceChangeSetIssueCode.PROVENANCE),
        ("change_set_id", WorkspaceChangeSetIssueCode.CHANGE_SET_ID),
    ),
)
def test_change_set_validation_detects_tampering(
    tamper: str, expected_code: WorkspaceChangeSetIssueCode
) -> None:
    artifact = _artifact()
    change_set, snapshot = _change_set(
        (_state(artifact.logical_name, "before\n"),), artifact
    )
    supplied_artifact = artifact
    if tamper == "workspace":
        change_set = change_set.model_copy(update={"workspace_id": "OTHER"})
    elif tamper == "snapshot":
        change_set = change_set.model_copy(update={"base_snapshot_id": "OLD"})
    elif tamper in {"request", "attempt", "task"}:
        field = {
            "request": "request_id",
            "attempt": "attempt_id",
            "task": "task_id",
        }[tamper]
        supplied_artifact = artifact.model_copy(update={field: "MISMATCH"})
    elif tamper == "change_set_id":
        change_set = change_set.model_copy(update={"change_set_id": "TAMPERED"})
    else:
        change = change_set.file_changes[0]
        updates: dict[str, object] = {
            "desired_hash": {"desired_content_hash": "0" * 64},
            "desired_content": {"desired_content": "tampered\n"},
            "preimage": {"expected_preimage_hash": "0" * 64},
            "operation": {"operation": WorkspaceChangeOperation.CREATE},
            "unsupported_operation": {"operation": "DELETE"},
            "artifact_lineage": {"artifact_lineage_id": "MISMATCH"},
        }[tamper]
        change_set = change_set.model_copy(
            update={"file_changes": (change.model_copy(update=updates),)}
        )

    result = _validate_change_set(
        change_set, snapshot, (supplied_artifact,)
    )

    assert result.passed is False
    assert expected_code in _codes(result)
    if tamper not in {"request", "attempt", "task"}:
        assert workspace_change_set_identity_is_valid(change_set) is False


def test_change_set_validation_detects_missing_and_tampered_artifacts() -> None:
    artifact = _artifact()
    change_set, snapshot = _change_set((), artifact)
    execution_validation = _validation(artifact)
    materialization = _materialization_validation(
        execution_validation, (artifact,)
    )

    missing = validate_workspace_change_set(
        change_set, snapshot, (), materialization
    )
    tampered = validate_workspace_change_set(
        change_set,
        snapshot,
        (artifact.model_copy(update={"artifact_type": EngineeringArtifactType.TEST}),),
        materialization,
    )

    assert WorkspaceChangeSetIssueCode.ARTIFACT_REFERENCE in _codes(missing)
    assert WorkspaceChangeSetIssueCode.ARTIFACT_REFERENCE in _codes(tampered)


def test_preimage_validation_accepts_matching_state_and_rejects_stale_modify() -> None:
    artifact = _artifact(content="after\n")
    change_set, base = _change_set(
        (_state(artifact.logical_name, "before\n"),), artifact
    )
    current = build_workspace_snapshot(
        "WORKSPACE-001", (_state(artifact.logical_name, "different\n"),)
    )

    valid = validate_workspace_change_set_preimages(change_set, base)
    stale = validate_workspace_change_set_preimages(change_set, current)

    assert valid.passed is True
    assert stale.passed is False
    assert _codes(stale) == {WorkspaceChangeSetIssueCode.STALE_PREIMAGE}


def test_preimage_validation_enforces_create_absence() -> None:
    artifact = _artifact()
    change_set, empty = _change_set((), artifact)
    occupied = build_workspace_snapshot(
        "WORKSPACE-001", (_state(artifact.logical_name, "occupied\n"),)
    )

    assert validate_workspace_change_set_preimages(change_set, empty).passed is True
    assert (
        validate_workspace_change_set_preimages(change_set, occupied).passed
        is False
    )


def test_preimage_validation_treats_changed_no_change_file_as_stale() -> None:
    artifact = _artifact(content="same\n")
    change_set, base = _change_set(
        (_state(artifact.logical_name, "same\n"),), artifact
    )
    changed = build_workspace_snapshot(
        "WORKSPACE-001", (_state(artifact.logical_name, "changed\n"),)
    )

    assert change_set.file_changes[0].operation is WorkspaceChangeOperation.NO_CHANGE
    assert validate_workspace_change_set_preimages(change_set, base).passed is True
    assert (
        validate_workspace_change_set_preimages(change_set, changed).passed
        is False
    )


def test_preimage_validation_rejects_tampered_comparison_snapshot_id() -> None:
    artifact = _artifact(content="same\n")
    change_set, comparison = _change_set(
        (_state(artifact.logical_name, "same\n"),), artifact
    )
    tampered = comparison.model_copy(update={"snapshot_id": "TAMPERED"})

    result = validate_workspace_change_set_preimages(change_set, tampered)

    assert result.passed is False
    assert _codes(result) == {WorkspaceChangeSetIssueCode.SNAPSHOT_ID}


def test_preimage_validation_rejects_noncanonical_snapshot_order() -> None:
    artifact = _artifact(path="src/a.py", content="same\n")
    change_set, comparison = _change_set(
        (
            _state("src/a.py", "same\n"),
            _state("src/b.py", "other\n"),
        ),
        artifact,
    )
    reordered = comparison.model_copy(
        update={"files": tuple(reversed(comparison.files))}
    )

    result = validate_workspace_change_set_preimages(change_set, reordered)

    assert result.passed is False
    assert _codes(result) == {WorkspaceChangeSetIssueCode.SNAPSHOT_ID}


def test_parallel_disjoint_paths_do_not_conflict() -> None:
    snapshot = build_workspace_snapshot("WORKSPACE-001")
    first = _artifact(path="src/api.py")
    second = _artifact(
        path="tests/test_api.py",
        artifact_id="ARTIFACT-002",
        lineage_id="artifact-lineage-002",
        task_id="TASK-002",
        request_id="REQUEST-002",
        attempt_id="ATTEMPT-002",
    )
    change_a = _build_change_set(snapshot, _validation(first), (first,))
    change_b = _build_change_set(snapshot, _validation(second), (second,))

    analysis = analyze_workspace_change_set_conflicts((change_a, change_b))

    assert analysis.has_conflicts is False
    assert analysis.conflicts == ()


@mark.parametrize("same_content", (False, True))
def test_parallel_same_path_mutations_conflict_even_when_identical(
    same_content: bool,
) -> None:
    snapshot = build_workspace_snapshot(
        "WORKSPACE-001", (_state("src/api.py", "before\n"),)
    )
    first = _artifact(path="src/api.py", content="after-a\n")
    second = _artifact(
        path="src/api.py",
        content="after-a\n" if same_content else "after-b\n",
        artifact_id="ARTIFACT-002",
        lineage_id="artifact-lineage-002",
        task_id="TASK-002",
        request_id="REQUEST-002",
        attempt_id="ATTEMPT-002",
    )
    change_a = _build_change_set(snapshot, _validation(first), (first,))
    change_b = _build_change_set(snapshot, _validation(second), (second,))

    analysis = analyze_workspace_change_set_conflicts((change_b, change_a))

    assert analysis.has_conflicts is True
    assert analysis.conflicts[0].path == "src/api.py"
    assert tuple(item.task_id for item in analysis.conflicts[0].participants) == (
        "TASK-001",
        "TASK-002",
    )


def test_parallel_create_create_and_structural_create_modify_conflict() -> None:
    snapshot = build_workspace_snapshot("WORKSPACE-001")
    first = _artifact(path="src/api.py")
    second = _artifact(
        path="src/api.py",
        artifact_id="ARTIFACT-002",
        lineage_id="artifact-lineage-002",
        task_id="TASK-002",
        request_id="REQUEST-002",
        attempt_id="ATTEMPT-002",
    )
    change_a = _build_change_set(snapshot, _validation(first), (first,))
    change_b = _build_change_set(snapshot, _validation(second), (second,))
    altered = change_b.model_copy(
        update={
            "file_changes": (
                change_b.file_changes[0].model_copy(
                    update={
                        "operation": WorkspaceChangeOperation.MODIFY,
                        "expected_preimage_hash": "0" * 64,
                    }
                ),
            )
        }
    )

    assert analyze_workspace_change_set_conflicts((change_a, change_b)).has_conflicts
    assert analyze_workspace_change_set_conflicts((change_a, altered)).has_conflicts


def test_no_change_pair_is_compatible_but_mutation_overlap_fails_closed() -> None:
    snapshot = build_workspace_snapshot(
        "WORKSPACE-001", (_state("src/api.py", "same\n"),)
    )
    first = _artifact(path="src/api.py", content="same\n")
    second = _artifact(
        path="src/api.py",
        content="same\n",
        artifact_id="ARTIFACT-002",
        lineage_id="artifact-lineage-002",
        task_id="TASK-002",
        request_id="REQUEST-002",
        attempt_id="ATTEMPT-002",
    )
    change_a = _build_change_set(snapshot, _validation(first), (first,))
    change_b = _build_change_set(snapshot, _validation(second), (second,))
    mutation = change_b.model_copy(
        update={
            "file_changes": (
                change_b.file_changes[0].model_copy(
                    update={"operation": WorkspaceChangeOperation.MODIFY}
                ),
            )
        }
    )

    assert not analyze_workspace_change_set_conflicts(
        (change_a, change_b)
    ).has_conflicts
    assert analyze_workspace_change_set_conflicts((change_a, mutation)).has_conflicts


def test_conflict_evidence_is_independent_of_input_order() -> None:
    snapshot = build_workspace_snapshot("WORKSPACE-001")
    artifacts = tuple(
        _artifact(
            path="src/shared.py",
            artifact_id=f"ARTIFACT-{index:03d}",
            lineage_id=f"artifact-lineage-{index:03d}",
            task_id=f"TASK-{index:03d}",
            request_id=f"REQUEST-{index:03d}",
            attempt_id=f"ATTEMPT-{index:03d}",
        )
        for index in (1, 2, 3)
    )
    change_sets = tuple(
        _build_change_set(snapshot, _validation(item), (item,))
        for item in artifacts
    )

    forward = analyze_workspace_change_set_conflicts(change_sets)
    reverse = analyze_workspace_change_set_conflicts(tuple(reversed(change_sets)))

    assert forward == reverse


def test_authoritative_workspace_contracts_are_frozen() -> None:
    change_set, snapshot = _change_set()

    with raises(ValidationError):
        snapshot.workspace_id = "OTHER"  # type: ignore[misc]
    with raises(ValidationError):
        change_set.file_changes[0].desired_content = "tampered"  # type: ignore[misc]
