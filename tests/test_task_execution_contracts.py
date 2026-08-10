"""Tests for deterministic task-execution contracts and artifact boundaries."""

from __future__ import annotations

from pydantic import ValidationError
from pytest import mark, raises

from agentic_sdlc.requirement_analysis import RequirementAnalysis
from agentic_sdlc.requirement_spec import (
    ApprovedRequirementSpec,
    build_approved_requirement_spec,
)
from agentic_sdlc.task_execution import (
    TaskExecutionRecoveryAction,
    TaskExecutionRecoveryFailureKind,
    TaskExecutionState,
    TaskExecutionStatus,
    TaskGraphExecutionState,
    TaskGraphExecutionStatus,
    decide_task_execution_recovery,
    initialize_task_graph_execution,
    mark_task_succeeded,
    prepare_task_retry,
    start_task,
)
from agentic_sdlc.task_execution_contracts import (
    ArtifactOutput,
    EngineeringArtifact,
    EngineeringArtifactType,
    TaskExecutionContractError,
    TaskExecutionCorrelationError,
    TaskExecutionRequest,
    TaskExecutionResult,
    TaskExecutionValidationResult,
    ValidationCheck,
    build_task_execution_request,
    canonicalize_execution_result,
    classify_validation_failure,
    validate_execution_result,
)
from agentic_sdlc.task_graph import (
    ProposedTask,
    ProposedTaskGraph,
    Task,
    TaskGraph,
    TaskMaterializationPolicy,
    TaskType,
    normalize_and_validate_task_graph,
)


FIXED_TIME = "2026-08-09T12:00:00+00:00"


def _analysis() -> RequirementAnalysis:
    return RequirementAnalysis(
        normalized_problem_statement=(
            "Build a governed URL shortener under the approved constraints."
        ),
        requirement_type="greenfield",
        functional_requirements=[
            "Accept a long URL.",
            "Redirect a short URL.",
        ],
        nonfunctional_requirements=["Short identifiers must be unique."],
        constraints=["No storage technology has been selected."],
        ambiguities=["Whether shortened URLs expire is unspecified."],
        assumptions=[
            "Short URLs do not expire unless a later approved specification "
            "says otherwise."
        ],
        acceptance_criteria=[
            "A valid URL receives a short URL.",
            "A known short URL redirects to its original URL.",
        ],
        risks=["Short-code collisions could redirect to the wrong URL."],
        needs_clarification=True,
        confidence=0.9,
    )


def _spec() -> ApprovedRequirementSpec:
    return build_approved_requirement_spec(
        _analysis(), source_analysis_revision=1, created_at=FIXED_TIME
    )


def _proposed_task(
    key: str,
    *,
    depends_on: tuple[str, ...] = (),
    requirement_refs: tuple[str, ...] = ("FR-001",),
    acceptance_refs: tuple[str, ...] = ("AC-001",),
    risk_refs: tuple[str, ...] = (),
    ambiguity_refs: tuple[str, ...] = (),
    expected_outputs: tuple[str, ...] | None = None,
) -> ProposedTask:
    return ProposedTask(
        key=key,
        title=key.replace("_", " ").title(),
        description=f"Produce the {key} engineering definition.",
        task_type=TaskType.DESIGN,
        materialization_policy=TaskMaterializationPolicy.FORBIDDEN,
        depends_on=list(depends_on),
        requirement_refs=list(requirement_refs),
        acceptance_criteria_refs=list(acceptance_refs),
        risk_refs=list(risk_refs),
        ambiguity_refs=list(ambiguity_refs),
        expected_outputs=list(
            expected_outputs
            if expected_outputs is not None
            else (f"{key}.md",)
        ),
    )


def _graph(
    spec: ApprovedRequirementSpec, *tasks: ProposedTask
) -> TaskGraph:
    graph, _ = normalize_and_validate_task_graph(
        ProposedTaskGraph(tasks=list(tasks)),
        spec,
        version=1,
        created_at=FIXED_TIME,
    )
    return graph


def _context_graph(spec: ApprovedRequirementSpec) -> TaskGraph:
    return _graph(
        spec,
        _proposed_task(
            "target",
            requirement_refs=("FR-001", "NFR-001", "CON-001"),
            acceptance_refs=("AC-001",),
            risk_refs=("RISK-001",),
            ambiguity_refs=("AMB-001",),
            expected_outputs=("api-design",),
        ),
        _proposed_task(
            "unrelated",
            requirement_refs=("FR-002",),
            acceptance_refs=("AC-002",),
        ),
    )


def _running_request(
    spec: ApprovedRequirementSpec,
    graph: TaskGraph,
    task_id: str = "TASK-001",
    *,
    execution: TaskGraphExecutionState | None = None,
    artifacts: tuple[EngineeringArtifact, ...] = (),
    validations: tuple[TaskExecutionValidationResult, ...] = (),
) -> tuple[TaskGraphExecutionState, TaskExecutionRequest]:
    active = execution or initialize_task_graph_execution(graph)
    if next(
        state for state in active.task_states if state.task_id == task_id
    ).status is TaskExecutionStatus.READY:
        active = start_task(graph, active, task_id)
    request = build_task_execution_request(
        spec,
        graph,
        active,
        task_id,
        accepted_artifacts=artifacts,
        dependency_validations=validations,
    )
    return active, request


def _result(
    request: TaskExecutionRequest,
    *outputs: ArtifactOutput,
    summary: str = "Proposed engineering output.",
) -> TaskExecutionResult:
    return TaskExecutionResult(
        request_id=request.request_id,
        attempt_id=request.attempt_id,
        task_id=request.task_id,
        summary=summary,
        outputs=tuple(outputs),
        assumptions=(),
        risks=(),
    )


def _output(
    logical_name: str = "api-design",
    content: str = "# API design\n",
    *,
    artifact_type: EngineeringArtifactType = EngineeringArtifactType.DESIGN,
) -> ArtifactOutput:
    return ArtifactOutput(
        artifact_type=artifact_type,
        logical_name=logical_name,
        content=content,
    )


def _canonical_output(
    spec: ApprovedRequirementSpec,
    graph: TaskGraph,
    execution: TaskGraphExecutionState,
    task_id: str,
    *,
    logical_name: str,
) -> tuple[
    TaskExecutionRequest,
    tuple[EngineeringArtifact, ...],
    TaskExecutionValidationResult,
]:
    request = build_task_execution_request(spec, graph, execution, task_id)
    result = _result(request, _output(logical_name=logical_name))
    artifacts = canonicalize_execution_result(
        request, result, created_at=FIXED_TIME
    )
    validation = validate_execution_result(request, result, artifacts)
    assert validation.passed is True
    return request, artifacts, validation


def _dependency_attempt(
    *outputs: ArtifactOutput,
) -> tuple[
    ApprovedRequirementSpec,
    TaskGraph,
    TaskGraphExecutionState,
    tuple[EngineeringArtifact, ...],
    TaskExecutionValidationResult,
]:
    spec = _spec()
    graph = _graph(
        spec,
        _proposed_task(
            "dependency",
            requirement_refs=("FR-001", "NFR-001", "CON-001"),
            acceptance_refs=("AC-001",),
        ),
        _proposed_task(
            "target",
            depends_on=("dependency",),
            requirement_refs=("FR-002",),
            acceptance_refs=("AC-002",),
        ),
    )
    execution = start_task(
        graph, initialize_task_graph_execution(graph), "TASK-001"
    )
    request = build_task_execution_request(spec, graph, execution, "TASK-001")
    result = _result(request, *outputs)
    artifacts = canonicalize_execution_result(
        request, result, created_at=FIXED_TIME
    )
    validation = validate_execution_result(request, result, artifacts)
    execution = mark_task_succeeded(graph, execution, "TASK-001")
    execution = start_task(graph, execution, "TASK-002")
    return spec, graph, execution, artifacts, validation


def test_request_identity_and_context_are_deterministic_for_running_attempt() -> None:
    spec = _spec()
    graph = _context_graph(spec)
    execution, first = _running_request(spec, graph)
    second = build_task_execution_request(spec, graph, execution, "TASK-001")

    assert first == second
    assert first.graph_id == graph.graph_id
    assert first.requirement_spec_id == spec.spec_id
    assert first.task_id == "TASK-001"
    assert first.attempt_number == 1
    assert first.task is graph.tasks[0]
    assert first.request_id
    assert first.attempt_id


def test_request_requires_running_task_and_positive_attempt_count() -> None:
    spec = _spec()
    graph = _context_graph(spec)
    ready = initialize_task_graph_execution(graph)

    invalid_running = TaskGraphExecutionState(
        graph_id=graph.graph_id,
        status=TaskGraphExecutionStatus.RUNNING,
        task_states=(
            TaskExecutionState(
                task_id="TASK-001",
                status=TaskExecutionStatus.RUNNING,
                attempt_count=0,
            ),
            ready.task_states[1],
        ),
    )
    with raises(TaskExecutionContractError, match="no started execution attempt"):
        build_task_execution_request(
            spec, graph, invalid_running, "TASK-001"
        )


@mark.parametrize(
    "task_status",
    (
        TaskExecutionStatus.BLOCKED,
        TaskExecutionStatus.READY,
        TaskExecutionStatus.SUCCEEDED,
        TaskExecutionStatus.FAILED,
    ),
)
def test_request_rejects_every_non_running_task_status(
    task_status: TaskExecutionStatus,
) -> None:
    spec = _spec()
    graph = _context_graph(spec)
    initial = initialize_task_graph_execution(graph)
    execution = TaskGraphExecutionState(
        graph_id=graph.graph_id,
        status=(
            TaskGraphExecutionStatus.FAILED
            if task_status is TaskExecutionStatus.FAILED
            else TaskGraphExecutionStatus.RUNNING
        ),
        task_states=(
            TaskExecutionState(
                task_id="TASK-001",
                status=task_status,
                attempt_count=(0 if task_status is TaskExecutionStatus.READY else 1),
            ),
            initial.task_states[1],
        ),
    )

    with raises(TaskExecutionContractError, match="must be RUNNING"):
        build_task_execution_request(spec, graph, execution, "TASK-001")


def test_request_resolves_only_referenced_approved_context() -> None:
    spec = _spec()
    graph = _context_graph(spec)
    _, request = _running_request(spec, graph)
    context = request.requirement_context

    assert context.normalized_problem_statement == (
        "Build a governed URL shortener under the approved constraints."
    )
    assert context.requirement_type == "greenfield"
    assert context.assumptions == (
        "Short URLs do not expire unless a later approved specification says "
        "otherwise.",
    )
    assert [item.item_id for item in context.functional_requirements] == ["FR-001"]
    assert [item.item_id for item in context.nonfunctional_requirements] == [
        "NFR-001"
    ]
    assert [item.item_id for item in context.constraints] == ["CON-001"]
    assert [item.item_id for item in context.acceptance_criteria] == ["AC-001"]
    assert [item.item_id for item in context.risks] == ["RISK-001"]
    assert [item.item_id for item in context.ambiguities] == ["AMB-001"]
    assert {item.item_id for item in context.all_items()}.isdisjoint(
        {"FR-002", "AC-002"}
    )


def test_request_rejects_unknown_approved_reference() -> None:
    spec = _spec()
    graph = _context_graph(spec)
    invalid_task = graph.tasks[0].model_copy(
        update={"requirement_refs": ("FR-999",)}
    )
    invalid_graph = graph.model_copy(
        update={"tasks": (invalid_task, graph.tasks[1])}
    )
    execution = start_task(
        invalid_graph,
        initialize_task_graph_execution(invalid_graph),
        "TASK-001",
    )

    with raises(TaskExecutionContractError, match="FR-999"):
        build_task_execution_request(
            spec, invalid_graph, execution, "TASK-001"
        )


def test_direct_dependency_artifact_is_available_to_running_task() -> None:
    spec = _spec()
    graph = _graph(
        spec,
        _proposed_task(
            "dependency",
            requirement_refs=("FR-001", "NFR-001", "CON-001"),
            acceptance_refs=("AC-001",),
        ),
        _proposed_task(
            "target",
            depends_on=("dependency",),
            requirement_refs=("FR-002",),
            acceptance_refs=("AC-002",),
        ),
    )
    execution = start_task(
        graph, initialize_task_graph_execution(graph), "TASK-001"
    )
    _, dependency_artifacts, dependency_validation = _canonical_output(
        spec,
        graph,
        execution,
        "TASK-001",
        logical_name="dependency-design",
    )
    execution = mark_task_succeeded(graph, execution, "TASK-001")
    execution = start_task(graph, execution, "TASK-002")

    request = build_task_execution_request(
        spec,
        graph,
        execution,
        "TASK-002",
        dependency_artifacts,
        (dependency_validation,),
    )

    assert request.dependency_artifacts == dependency_artifacts
    assert dependency_validation.artifact_ids == tuple(
        artifact.artifact_id for artifact in dependency_artifacts
    )

    forged = dependency_artifacts[0].model_copy(
        update={"requirement_refs": ("FR-999",)}
    )
    with raises(TaskExecutionContractError, match="invalid source provenance"):
        build_task_execution_request(
            spec,
            graph,
            execution,
            "TASK-002",
            (forged,),
            (dependency_validation,),
        )


def test_unrelated_artifact_is_rejected_as_dependency_context() -> None:
    spec = _spec()
    graph = _graph(
        spec,
        _proposed_task(
            "dependency",
            requirement_refs=("FR-001", "NFR-001", "CON-001"),
            acceptance_refs=("AC-001",),
        ),
        _proposed_task(
            "target",
            depends_on=("dependency",),
            requirement_refs=("FR-002",),
            acceptance_refs=("AC-002",),
        ),
        _proposed_task("unrelated"),
    )
    execution = initialize_task_graph_execution(graph)
    execution = start_task(graph, execution, "TASK-001")
    execution = start_task(graph, execution, "TASK-003")
    _, dependency_artifacts, dependency_validation = _canonical_output(
        spec, graph, execution, "TASK-001", logical_name="dependency"
    )
    _, unrelated_artifacts, unrelated_validation = _canonical_output(
        spec, graph, execution, "TASK-003", logical_name="unrelated"
    )
    execution = mark_task_succeeded(graph, execution, "TASK-001")
    execution = mark_task_succeeded(graph, execution, "TASK-003")
    execution = start_task(graph, execution, "TASK-002")

    with raises(TaskExecutionContractError, match="not a direct dependency"):
        build_task_execution_request(
            spec,
            graph,
            execution,
            "TASK-002",
            unrelated_artifacts,
            (unrelated_validation,),
        )

    request = build_task_execution_request(
        spec,
        graph,
        execution,
        "TASK-002",
        dependency_artifacts,
        (dependency_validation,),
    )
    assert request.dependency_artifacts == dependency_artifacts


def test_fan_in_dependency_artifacts_are_sorted_by_declared_dependency_order() -> None:
    spec = _spec()
    graph = _graph(
        spec,
        _proposed_task(
            "first",
            requirement_refs=("FR-001", "NFR-001", "CON-001"),
            acceptance_refs=("AC-001",),
        ),
        _proposed_task("second"),
        _proposed_task(
            "join",
            depends_on=("first", "second"),
            requirement_refs=("FR-002",),
            acceptance_refs=("AC-002",),
        ),
    )
    execution = initialize_task_graph_execution(graph)
    execution = start_task(graph, execution, "TASK-001")
    execution = start_task(graph, execution, "TASK-002")
    _, first_artifacts, first_validation = _canonical_output(
        spec, graph, execution, "TASK-001", logical_name="first"
    )
    _, second_artifacts, second_validation = _canonical_output(
        spec, graph, execution, "TASK-002", logical_name="second"
    )
    execution = mark_task_succeeded(graph, execution, "TASK-001")
    execution = mark_task_succeeded(graph, execution, "TASK-002")
    execution = start_task(graph, execution, "TASK-003")

    request = build_task_execution_request(
        spec,
        graph,
        execution,
        "TASK-003",
        (*second_artifacts, *first_artifacts),
        (second_validation, first_validation),
    )

    assert [artifact.task_id for artifact in request.dependency_artifacts] == [
        "TASK-001",
        "TASK-002",
    ]
    with raises(TaskExecutionContractError, match="Missing successful validation"):
        build_task_execution_request(
            spec,
            graph,
            execution,
            "TASK-003",
            first_artifacts,
            (first_validation,),
        )


def test_executor_models_forbid_authoritative_fields() -> None:
    output_data = _output().model_dump(mode="python")
    output_data["artifact_id"] = "executor-controlled"
    with raises(ValidationError):
        ArtifactOutput.model_validate(output_data)

    spec = _spec()
    graph = _context_graph(spec)
    _, request = _running_request(spec, graph)
    result_data = _result(request, _output()).model_dump(mode="python")
    for forbidden_field in (
        "passed",
        "success",
        "artifact_id",
        "lineage_id",
        "content_hash",
        "graph_status",
        "task_status",
    ):
        with raises(ValidationError):
            TaskExecutionResult.model_validate(
                {**result_data, forbidden_field: True}
            )


def test_result_correlation_mismatch_is_rejected_and_validates_false() -> None:
    spec = _spec()
    graph = _context_graph(spec)
    _, request = _running_request(spec, graph)
    mismatched = _result(request, _output()).model_copy(
        update={"attempt_id": "wrong-attempt"}
    )

    with raises(TaskExecutionContractError, match="attempt_id"):
        canonicalize_execution_result(
            request, mismatched, created_at=FIXED_TIME
        )

    validation = validate_execution_result(request, mismatched, ())
    assert validation.passed is False
    assert any(
        check.name == "request_correlation" and not check.passed
        for check in validation.checks
    )


def test_canonical_artifacts_have_stable_distinct_identity_and_provenance() -> None:
    spec = _spec()
    graph = _context_graph(spec)
    _, request = _running_request(spec, graph)
    result = _result(
        request,
        _output(logical_name="same", content="identical"),
        _output(logical_name="same", content="identical"),
    )

    first = canonicalize_execution_result(
        request, result, created_at=FIXED_TIME
    )
    second = canonicalize_execution_result(
        request, result, created_at=FIXED_TIME
    )

    assert first == second
    assert first[0].artifact_id != first[1].artifact_id
    assert first[0].lineage_id != first[1].lineage_id
    assert first[0].content_hash != first[1].content_hash
    assert first[0].requirement_spec_id == spec.spec_id
    assert first[0].graph_id == graph.graph_id
    assert first[0].task_id == request.task_id
    assert first[0].request_id == request.request_id
    assert first[0].attempt_id == request.attempt_id
    assert first[0].attempt_number == 1
    assert first[0].content == "identical"


def test_new_attempt_changes_identity_but_keeps_slot_lineage() -> None:
    spec = _spec()
    graph = _context_graph(spec)
    first_execution, first_request = _running_request(spec, graph)
    recovery = decide_task_execution_recovery(
        task_id="TASK-001",
        attempt_number=1,
        request_id=first_request.request_id,
        attempt_id=first_request.attempt_id,
        failure_kind=TaskExecutionRecoveryFailureKind.VALIDATION,
        retryable=True,
        feedback="Blank artifact contents at output positions: 1.",
    )
    second_execution = start_task(
        graph,
        prepare_task_retry(graph, first_execution, "TASK-001"),
        "TASK-001",
    )
    second_request = build_task_execution_request(
        spec, graph, second_execution, "TASK-001", prior_recovery_decision=recovery
    )
    first_artifact = canonicalize_execution_result(
        first_request,
        _result(first_request, _output()),
        created_at=FIXED_TIME,
    )[0]
    second_artifact = canonicalize_execution_result(
        second_request,
        _result(second_request, _output()),
        created_at=FIXED_TIME,
    )[0]

    assert first_request.attempt_id != second_request.attempt_id
    assert first_request.request_id != second_request.request_id
    assert first_artifact.artifact_id != second_artifact.artifact_id
    assert first_artifact.content_hash != second_artifact.content_hash
    assert first_artifact.lineage_id == second_artifact.lineage_id
    assert first_request.retry_context is None
    assert second_request.retry_context is not None
    assert second_request.retry_context.prior_request_id == first_request.request_id
    assert second_request.retry_context.feedback == recovery.feedback


def test_retry_request_requires_exact_authoritative_prior_retry_decision() -> None:
    spec = _spec()
    graph = _context_graph(spec)
    first_execution, first_request = _running_request(spec, graph)
    second_execution = start_task(
        graph,
        prepare_task_retry(graph, first_execution, "TASK-001"),
        "TASK-001",
    )
    valid = decide_task_execution_recovery(
        task_id="TASK-001",
        attempt_number=1,
        request_id=first_request.request_id,
        attempt_id=first_request.attempt_id,
        failure_kind=TaskExecutionRecoveryFailureKind.EXECUTOR,
        retryable=True,
        feedback="Transient provider failure.",
    )

    with raises(TaskExecutionContractError, match="first task attempt"):
        build_task_execution_request(
            spec,
            graph,
            first_execution,
            "TASK-001",
            prior_recovery_decision=valid,
        )

    with raises(TaskExecutionContractError, match="requires its immediately prior"):
        build_task_execution_request(spec, graph, second_execution, "TASK-001")

    invalid = (
        valid.model_copy(update={"task_id": "TASK-002"}),
        valid.model_copy(update={"attempt_number": 2}),
        valid.model_copy(update={"action": TaskExecutionRecoveryAction.FAIL_TASK}),
        valid.model_copy(update={"request_id": "wrong-request"}),
    )
    for decision in invalid:
        with raises(TaskExecutionContractError, match="does not authorize"):
            build_task_execution_request(
                spec,
                graph,
                second_execution,
                "TASK-001",
                prior_recovery_decision=decision,
            )


def test_correlation_error_is_typed_and_other_contract_errors_are_not() -> None:
    spec = _spec()
    graph = _context_graph(spec)
    _, request = _running_request(spec, graph)
    mismatched = _result(request, _output()).model_copy(
        update={"attempt_id": "wrong-attempt"}
    )

    with raises(TaskExecutionCorrelationError):
        canonicalize_execution_result(request, mismatched, created_at=FIXED_TIME)
    with raises(TaskExecutionContractError) as raised:
        canonicalize_execution_result(request, _result(request), created_at=" ")
    assert not isinstance(raised.value, TaskExecutionCorrelationError)


def test_validation_retry_allowlist_is_explicit_and_conservative() -> None:
    spec = _spec()
    graph = _context_graph(spec)
    _, request = _running_request(spec, graph)
    result = _result(request, _output(logical_name=" ", content=""))
    artifacts = canonicalize_execution_result(
        request, result, created_at=FIXED_TIME
    )
    validation = validate_execution_result(request, result, artifacts)

    retryable, feedback = classify_validation_failure(validation)
    assert retryable is True
    assert "Blank artifact logical names" in feedback
    assert "Blank artifact contents" in feedback

    mixed = validation.model_copy(
        update={
            "checks": (
                *validation.checks,
                ValidationCheck(
                    name="future_unknown_check",
                    passed=False,
                    detail="Unknown future failure.",
                ),
            )
        }
    )
    assert classify_validation_failure(mixed)[0] is False

    count_failure = validate_execution_result(request, result, ())
    assert classify_validation_failure(count_failure)[0] is False


def test_artifact_content_change_changes_hash_but_not_semantic_slot_lineage() -> None:
    spec = _spec()
    graph = _context_graph(spec)
    _, request = _running_request(spec, graph)
    first = canonicalize_execution_result(
        request,
        _result(request, _output(content="first")),
        created_at=FIXED_TIME,
    )[0]
    second = canonicalize_execution_result(
        request,
        _result(request, _output(content="second")),
        created_at=FIXED_TIME,
    )[0]

    assert first.content_hash != second.content_hash
    assert first.artifact_id != second.artifact_id
    assert first.lineage_id == second.lineage_id


def test_valid_result_passes_without_transitioning_scheduler_state() -> None:
    spec = _spec()
    graph = _context_graph(spec)
    execution, request = _running_request(spec, graph)
    result = _result(request, _output())
    artifacts = canonicalize_execution_result(
        request, result, created_at=FIXED_TIME
    )

    validation = validate_execution_result(request, result, artifacts)

    assert validation.passed is True
    assert validation.artifact_ids == tuple(
        artifact.artifact_id for artifact in artifacts
    )
    assert validation.errors == ()
    assert all(check.passed for check in validation.checks)
    assert execution.task_states[0].status is TaskExecutionStatus.RUNNING
    assert execution.status is TaskGraphExecutionStatus.RUNNING


def test_missing_expected_output_fails_separate_validation() -> None:
    spec = _spec()
    graph = _context_graph(spec)
    _, request = _running_request(spec, graph)
    result = _result(request)
    artifacts = canonicalize_execution_result(
        request, result, created_at=FIXED_TIME
    )

    validation = validate_execution_result(request, result, artifacts)

    assert artifacts == ()
    assert validation.passed is False
    assert validation.artifact_ids == ()
    assert any(
        check.name == "expected_output_presence" and not check.passed
        for check in validation.checks
    )


def test_validation_rejects_missing_canonical_artifact() -> None:
    spec = _spec()
    graph = _context_graph(spec)
    _, request = _running_request(spec, graph)
    result = _result(request, _output())

    validation = validate_execution_result(request, result, ())

    assert validation.passed is False
    assert any(
        check.name == "artifact_count" and not check.passed
        for check in validation.checks
    )


def test_blank_output_fields_fail_validation_after_canonicalization() -> None:
    spec = _spec()
    graph = _context_graph(spec)
    _, request = _running_request(spec, graph)
    result = _result(request, _output(logical_name=" ", content=""))
    artifacts = canonicalize_execution_result(
        request, result, created_at=FIXED_TIME
    )

    validation = validate_execution_result(request, result, artifacts)

    assert validation.passed is False
    assert artifacts[0].logical_name == " "
    assert artifacts[0].content == ""
    assert {check.name for check in validation.checks if not check.passed} == {
        "logical_names",
        "artifact_contents",
    }


def test_forged_artifact_provenance_and_identity_fail_validation() -> None:
    spec = _spec()
    graph = _context_graph(spec)
    _, request = _running_request(spec, graph)
    result = _result(request, _output())
    artifact = canonicalize_execution_result(
        request, result, created_at=FIXED_TIME
    )[0]
    forged = artifact.model_copy(
        update={"graph_id": "GRAPH-OTHER", "artifact_id": "executor-value"}
    )

    validation = validate_execution_result(request, result, (forged,))

    assert validation.passed is False
    failed_checks = {check.name for check in validation.checks if not check.passed}
    assert "artifact_provenance" in failed_checks
    assert "artifact_identity" in failed_checks


def test_failed_validation_retains_produced_artifact_without_acceptance_state() -> None:
    spec = _spec()
    graph = _context_graph(spec)
    execution, request = _running_request(spec, graph)
    result = _result(request, _output(content=""))
    artifacts = canonicalize_execution_result(
        request, result, created_at=FIXED_TIME
    )

    validation = validate_execution_result(request, result, artifacts)

    assert validation.passed is False
    assert validation.artifact_ids == (artifacts[0].artifact_id,)
    assert len(artifacts) == 1
    assert artifacts[0].content == ""
    assert not hasattr(artifacts[0], "accepted")
    assert execution.task_states[0].status is TaskExecutionStatus.RUNNING
    assert execution.status is TaskGraphExecutionStatus.RUNNING


def test_dependency_builder_rejects_artifact_from_nonaccepted_attempt() -> None:
    spec = _spec()
    graph = _graph(
        spec,
        _proposed_task(
            "dependency",
            requirement_refs=("FR-001", "NFR-001", "CON-001"),
            acceptance_refs=("AC-001",),
        ),
        _proposed_task(
            "target",
            depends_on=("dependency",),
            requirement_refs=("FR-002",),
            acceptance_refs=("AC-002",),
        ),
    )
    execution = start_task(
        graph, initialize_task_graph_execution(graph), "TASK-001"
    )
    _, artifacts, validation = _canonical_output(
        spec, graph, execution, "TASK-001", logical_name="dependency"
    )
    execution = mark_task_succeeded(graph, execution, "TASK-001")
    execution = start_task(graph, execution, "TASK-002")
    stale = artifacts[0].model_copy(update={"attempt_number": 2})

    with raises(TaskExecutionContractError, match="accepted attempt"):
        build_task_execution_request(
            spec,
            graph,
            execution,
            "TASK-002",
            (stale,),
            (validation,),
        )


def test_source_success_without_validation_cannot_authorize_dependency_artifacts(
) -> None:
    spec, graph, execution, artifacts, validation = _dependency_attempt(
        _output(logical_name="dependency")
    )

    assert validation.passed is True
    with raises(TaskExecutionContractError, match="Missing successful validation"):
        build_task_execution_request(
            spec, graph, execution, "TASK-002", artifacts
        )


def test_whole_dependency_cannot_be_omitted_from_request_context() -> None:
    spec, graph, execution, artifacts, validation = _dependency_attempt(
        _output(logical_name="dependency")
    )

    assert artifacts
    assert validation.passed is True
    with raises(TaskExecutionContractError, match="direct dependencies: TASK-001"):
        build_task_execution_request(spec, graph, execution, "TASK-002")


def test_failed_validation_cannot_authorize_dependency_artifacts() -> None:
    spec, graph, execution, artifacts, validation = _dependency_attempt(
        _output(logical_name="dependency", content="")
    )

    assert validation.passed is False
    assert validation.artifact_ids == tuple(
        artifact.artifact_id for artifact in artifacts
    )
    with raises(TaskExecutionContractError, match="did not pass validation"):
        build_task_execution_request(
            spec,
            graph,
            execution,
            "TASK-002",
            artifacts,
            (validation,),
        )


def test_dependency_validation_correlation_and_artifact_set_must_match() -> None:
    spec, graph, execution, artifacts, validation = _dependency_attempt(
        _output(logical_name="dependency")
    )
    mismatched_validations = (
        validation.model_copy(update={"request_id": "wrong-request"}),
        validation.model_copy(update={"attempt_id": "wrong-attempt"}),
        validation.model_copy(update={"task_id": "TASK-999"}),
        validation.model_copy(update={"artifact_ids": ("ARTIFACT-OTHER",)}),
    )

    for mismatched in mismatched_validations:
        with raises(TaskExecutionContractError):
            build_task_execution_request(
                spec,
                graph,
                execution,
                "TASK-002",
                artifacts,
                (mismatched,),
            )


def test_partial_or_duplicate_validated_artifact_set_is_rejected() -> None:
    spec, graph, execution, artifacts, validation = _dependency_attempt(
        _output(logical_name="design", content="design"),
        _output(
            logical_name="schema",
            content="schema",
            artifact_type=EngineeringArtifactType.SCHEMA,
        ),
    )

    assert validation.artifact_ids == tuple(
        artifact.artifact_id for artifact in artifacts
    )
    with raises(TaskExecutionContractError, match="does not exactly match"):
        build_task_execution_request(
            spec,
            graph,
            execution,
            "TASK-002",
            artifacts[:1],
            (validation,),
        )
    with raises(TaskExecutionContractError, match="unique artifact IDs"):
        build_task_execution_request(
            spec,
            graph,
            execution,
            "TASK-002",
            (artifacts[0], artifacts[0]),
            (validation,),
        )

    request = build_task_execution_request(
        spec,
        graph,
        execution,
        "TASK-002",
        tuple(reversed(artifacts)),
        (validation,),
    )
    assert request.dependency_artifacts == artifacts


def test_explicitly_validated_empty_dependency_artifact_set_is_supported() -> None:
    spec = _spec()
    graph = _graph(
        spec,
        _proposed_task(
            "dependency",
            requirement_refs=("FR-001", "NFR-001", "CON-001"),
            acceptance_refs=("AC-001",),
            expected_outputs=(),
        ),
        _proposed_task(
            "target",
            depends_on=("dependency",),
            requirement_refs=("FR-002",),
            acceptance_refs=("AC-002",),
        ),
    )
    execution = start_task(
        graph, initialize_task_graph_execution(graph), "TASK-001"
    )
    source_request = build_task_execution_request(
        spec, graph, execution, "TASK-001"
    )
    source_result = _result(source_request)
    source_artifacts = canonicalize_execution_result(
        source_request, source_result, created_at=FIXED_TIME
    )
    source_validation = validate_execution_result(
        source_request, source_result, source_artifacts
    )
    execution = mark_task_succeeded(graph, execution, "TASK-001")
    execution = start_task(graph, execution, "TASK-002")

    assert source_artifacts == ()
    assert source_validation.passed is True
    assert source_validation.artifact_ids == ()
    request = build_task_execution_request(
        spec,
        graph,
        execution,
        "TASK-002",
        dependency_validations=(source_validation,),
    )
    assert request.dependency_artifacts == ()


def test_root_task_requires_no_dependency_evidence_and_rejects_extra_validation(
) -> None:
    spec = _spec()
    graph = _context_graph(spec)
    execution, request = _running_request(spec, graph)
    result = _result(request, _output())
    artifacts = canonicalize_execution_result(
        request, result, created_at=FIXED_TIME
    )
    validation = validate_execution_result(request, result, artifacts)

    assert build_task_execution_request(
        spec, graph, execution, "TASK-001"
    ) == request
    with raises(TaskExecutionContractError, match="not for a direct dependency"):
        build_task_execution_request(
            spec,
            graph,
            execution,
            "TASK-001",
            dependency_validations=(validation,),
        )
