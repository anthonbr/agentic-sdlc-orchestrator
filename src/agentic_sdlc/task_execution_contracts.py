"""Deterministic execution contracts and canonical engineering artifacts."""

from __future__ import annotations

import hashlib
import json
from enum import StrEnum
from typing import Literal
from uuid import uuid5

from pydantic import BaseModel, ConfigDict, Field

from agentic_sdlc.project_delivery import ProjectDeliverableRole
from agentic_sdlc.requirement_spec import (
    LINEAGE_NAMESPACE,
    ApprovedRequirementSpec,
    RequirementSpecItem,
)
from agentic_sdlc.task_execution import (
    MAX_TASK_EXECUTION_ATTEMPTS,
    TaskExecutionError,
    TaskExecutionRecoveryAction,
    TaskExecutionRecoveryDecision,
    TaskExecutionRecoveryFailureKind,
    TaskExecutionState,
    TaskExecutionStatus,
    TaskGraphExecutionState,
    TaskGraphExecutionStatus,
)
from agentic_sdlc.task_graph import Task, TaskGraph


class EngineeringArtifactType(StrEnum):
    """Restrained semantic classifications for proposed engineering output."""

    DESIGN = "DESIGN"
    SOURCE = "SOURCE"
    TEST = "TEST"
    SCHEMA = "SCHEMA"
    DOCUMENTATION = "DOCUMENTATION"
    VALIDATION = "VALIDATION"
    OTHER = "OTHER"


class ArtifactOutput(BaseModel):
    """Non-authoritative semantic output proposed by a bounded executor."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    artifact_type: EngineeringArtifactType
    logical_name: str
    content: str


class ArtifactMaterializationProposal(BaseModel):
    """Non-authoritative proposal to materialize one semantic output by ordinal."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    output_index: int = Field(ge=1)
    target_path: str = Field(min_length=1)


class EngineeringArtifact(BaseModel):
    """Immutable application-canonicalized engineering output with provenance."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    artifact_id: str
    lineage_id: str
    artifact_type: EngineeringArtifactType
    logical_name: str
    content: str
    content_hash: str
    output_index: int = Field(ge=1)
    requirement_spec_id: str
    graph_id: str
    task_id: str
    request_id: str
    attempt_id: str
    attempt_number: int = Field(ge=1)
    requirement_refs: tuple[str, ...]
    acceptance_criteria_refs: tuple[str, ...]
    risk_refs: tuple[str, ...]
    ambiguity_refs: tuple[str, ...]
    created_at: str


class TaskRequirementContext(BaseModel):
    """Approved global context plus exact items referenced by one task."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    normalized_problem_statement: str
    requirement_type: Literal["greenfield", "brownfield", "ambiguous"]
    assumptions: tuple[str, ...]
    functional_requirements: tuple[RequirementSpecItem, ...]
    nonfunctional_requirements: tuple[RequirementSpecItem, ...]
    constraints: tuple[RequirementSpecItem, ...]
    acceptance_criteria: tuple[RequirementSpecItem, ...]
    risks: tuple[RequirementSpecItem, ...]
    ambiguities: tuple[RequirementSpecItem, ...]

    def all_items(self) -> tuple[RequirementSpecItem, ...]:
        """Return resolved items in stable namespace order."""

        return (
            *self.functional_requirements,
            *self.nonfunctional_requirements,
            *self.constraints,
            *self.acceptance_criteria,
            *self.risks,
            *self.ambiguities,
        )


class TaskExecutionRetryContext(BaseModel):
    """Application-owned recovery context for the prior unsuccessful attempt."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    prior_attempt_number: int = Field(ge=1)
    prior_request_id: str
    prior_attempt_id: str
    failure_kind: TaskExecutionRecoveryFailureKind
    feedback: str


class TaskExecutionRequest(BaseModel):
    """Application-owned context for one running canonical task attempt."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    request_id: str
    attempt_id: str
    graph_id: str
    requirement_spec_id: str
    task_id: str
    attempt_number: int = Field(ge=1)
    task: Task
    requirement_context: TaskRequirementContext
    dependency_artifacts: tuple[EngineeringArtifact, ...]
    retry_context: TaskExecutionRetryContext | None = None


class TaskExecutionResult(BaseModel):
    """Non-authoritative semantic proposal returned by a bounded executor."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    request_id: str
    attempt_id: str
    task_id: str
    summary: str
    outputs: tuple[ArtifactOutput, ...]
    materialization_proposals: tuple[ArtifactMaterializationProposal, ...] = ()
    assumptions: tuple[str, ...]
    risks: tuple[str, ...]


class ValidationCheck(BaseModel):
    """One deterministic application-owned execution-result check."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    name: str
    passed: bool
    detail: str


class TaskExecutionValidationResult(BaseModel):
    """Authoritative judgment bound to an exact canonical artifact sequence."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    request_id: str
    attempt_id: str
    task_id: str
    passed: bool
    artifact_ids: tuple[str, ...]
    checks: tuple[ValidationCheck, ...]
    errors: tuple[str, ...]


class TaskExecutionContractError(TaskExecutionError):
    """Raised when execution context or artifact correlation is invalid."""


class TaskExecutionCorrelationError(TaskExecutionContractError):
    """Raised when executor-produced correlation differs from its request."""


RETRYABLE_VALIDATION_CHECKS = frozenset(
    {
        "expected_output_presence",
        "logical_names",
        "artifact_contents",
        "runnable_entrypoint_deliverable",
        "automated_tests_deliverable",
        "run_instructions_deliverable",
    }
)


def build_task_execution_request(
    requirement_spec: ApprovedRequirementSpec,
    task_graph: TaskGraph,
    execution_state: TaskGraphExecutionState,
    task_id: str,
    accepted_artifacts: tuple[EngineeringArtifact, ...] = (),
    dependency_validations: tuple[TaskExecutionValidationResult, ...] = (),
    *,
    prior_recovery_decision: TaskExecutionRecoveryDecision | None = None,
) -> TaskExecutionRequest:
    """Build authoritative context for one already-running task attempt."""

    _validate_spec_and_execution(requirement_spec, task_graph, execution_state)
    tasks_by_id = {task.task_id: task for task in task_graph.tasks}
    task = tasks_by_id.get(task_id)
    if task is None:
        raise TaskExecutionContractError(f"Unknown task ID: {task_id}.")

    states_by_id = _execution_states_by_id(execution_state)
    task_state = states_by_id[task_id]
    if task_state.status is not TaskExecutionStatus.RUNNING:
        raise TaskExecutionContractError(
            f"Task {task_id} must be RUNNING to build an execution request; "
            f"found {task_state.status.value}."
        )
    if task_state.attempt_count < 1:
        raise TaskExecutionContractError(
            f"Task {task_id} has no started execution attempt."
        )
    if execution_state.status not in {
        TaskGraphExecutionStatus.RUNNING,
        TaskGraphExecutionStatus.FAILED,
    }:
        raise TaskExecutionContractError(
            "A running task request requires a RUNNING or FAILED graph execution; "
            f"found {execution_state.status.value}."
        )

    for dependency_id in task.depends_on:
        dependency_state = states_by_id[dependency_id]
        if dependency_state.status is not TaskExecutionStatus.SUCCEEDED:
            raise TaskExecutionContractError(
                f"Direct dependency {dependency_id} is not SUCCEEDED."
            )

    attempt_id = _attempt_id(
        task_graph.graph_id, task.task_id, task_state.attempt_count
    )
    request_id = _request_id(requirement_spec.spec_id, attempt_id)
    retry_context = _derive_retry_context(
        requirement_spec=requirement_spec,
        task_graph=task_graph,
        task_state=task_state,
        prior_recovery_decision=prior_recovery_decision,
    )
    return TaskExecutionRequest(
        request_id=request_id,
        attempt_id=attempt_id,
        graph_id=task_graph.graph_id,
        requirement_spec_id=requirement_spec.spec_id,
        task_id=task.task_id,
        attempt_number=task_state.attempt_count,
        task=task,
        requirement_context=_resolve_requirement_context(requirement_spec, task),
        dependency_artifacts=_validated_dependency_artifacts(
            requirement_spec,
            task_graph,
            states_by_id,
            task,
            accepted_artifacts,
            dependency_validations,
        ),
        retry_context=retry_context,
    )


def canonicalize_execution_result(
    request: TaskExecutionRequest,
    result: TaskExecutionResult,
    *,
    created_at: str,
) -> tuple[EngineeringArtifact, ...]:
    """Convert semantic outputs to canonical immutable artifacts without I/O."""

    mismatches = _correlation_mismatches(request, result)
    if mismatches:
        raise TaskExecutionCorrelationError(
            "Execution result correlation mismatch: " + ", ".join(mismatches) + "."
        )
    if not created_at.strip():
        raise TaskExecutionContractError("Artifact creation timestamp is required.")

    artifacts: list[EngineeringArtifact] = []
    for output_index, output in enumerate(result.outputs, start=1):
        content_hash = _artifact_content_hash(
            request=request,
            output=output,
            output_index=output_index,
        )
        artifacts.append(
            EngineeringArtifact(
                artifact_id=_artifact_id(content_hash, output_index),
                lineage_id=_artifact_lineage_id(
                    request.task, output, output_index
                ),
                artifact_type=output.artifact_type,
                logical_name=output.logical_name,
                content=output.content,
                content_hash=content_hash,
                output_index=output_index,
                requirement_spec_id=request.requirement_spec_id,
                graph_id=request.graph_id,
                task_id=request.task_id,
                request_id=request.request_id,
                attempt_id=request.attempt_id,
                attempt_number=request.attempt_number,
                requirement_refs=request.task.requirement_refs,
                acceptance_criteria_refs=request.task.acceptance_criteria_refs,
                risk_refs=request.task.risk_refs,
                ambiguity_refs=request.task.ambiguity_refs,
                created_at=created_at,
            )
        )
    return tuple(artifacts)


def validate_execution_result(
    request: TaskExecutionRequest,
    result: TaskExecutionResult,
    artifacts: tuple[EngineeringArtifact, ...],
) -> TaskExecutionValidationResult:
    """Return a deterministic judgment without changing scheduler state."""

    checks: list[ValidationCheck] = []
    errors: list[str] = []

    def record(name: str, passed: bool, detail: str) -> None:
        checks.append(ValidationCheck(name=name, passed=passed, detail=detail))
        if not passed:
            errors.append(detail)

    correlation_mismatches = _correlation_mismatches(request, result)
    record(
        "request_correlation",
        not correlation_mismatches,
        (
            "Execution result correlation matches the request."
            if not correlation_mismatches
            else "Execution result correlation mismatch: "
            + ", ".join(correlation_mismatches)
            + "."
        ),
    )

    output_required = bool(request.task.expected_outputs)
    has_required_output = not output_required or bool(result.outputs)
    record(
        "expected_output_presence",
        has_required_output,
        (
            "Required executor output is present."
            if has_required_output
            else "Task declares expected outputs but the executor returned none."
        ),
    )

    blank_logical_names = tuple(
        index
        for index, output in enumerate(result.outputs, start=1)
        if not output.logical_name.strip()
    )
    record(
        "logical_names",
        not blank_logical_names,
        (
            "Artifact logical names are non-empty."
            if not blank_logical_names
            else "Blank artifact logical names at output positions: "
            + ", ".join(str(index) for index in blank_logical_names)
            + "."
        ),
    )

    blank_contents = tuple(
        index
        for index, output in enumerate(result.outputs, start=1)
        if not output.content.strip()
    )
    record(
        "artifact_contents",
        not blank_contents,
        (
            "Artifact contents are non-empty."
            if not blank_contents
            else "Blank artifact contents at output positions: "
            + ", ".join(str(index) for index in blank_contents)
            + "."
        ),
    )

    counts_match = len(result.outputs) == len(artifacts)
    record(
        "artifact_count",
        counts_match,
        (
            "Executor output count matches canonical artifact count."
            if counts_match
            else "Executor output count does not match canonical artifact count."
        ),
    )

    provenance_errors = _artifact_provenance_errors(request, artifacts)
    record(
        "artifact_provenance",
        not provenance_errors,
        (
            "Canonical artifact provenance matches the execution request."
            if not provenance_errors
            else "Artifact provenance mismatch: "
            + "; ".join(provenance_errors)
            + "."
        ),
    )

    identity_errors = _artifact_identity_errors(request, artifacts)
    record(
        "artifact_identity",
        not identity_errors,
        (
            "Canonical artifact identities and hashes are valid."
            if not identity_errors
            else "Artifact identity mismatch: "
            + "; ".join(identity_errors)
            + "."
        ),
    )

    correspondence_errors = _artifact_correspondence_errors(result, artifacts)
    record(
        "output_correspondence",
        not correspondence_errors,
        (
            "Canonical artifacts preserve executor output order and content."
            if not correspondence_errors
            else "Artifact/output mismatch: "
            + "; ".join(correspondence_errors)
            + "."
        ),
    )

    for role in request.task.deliverable_roles:
        name, passed, detail = _deliverable_role_validation(
            role, artifacts, result
        )
        record(name, passed, detail)

    return TaskExecutionValidationResult(
        request_id=request.request_id,
        attempt_id=request.attempt_id,
        task_id=request.task_id,
        passed=all(check.passed for check in checks),
        artifact_ids=tuple(artifact.artifact_id for artifact in artifacts),
        checks=tuple(checks),
        errors=tuple(errors),
    )


def _deliverable_role_validation(
    role: ProjectDeliverableRole,
    artifacts: tuple[EngineeringArtifact, ...],
    result: TaskExecutionResult,
) -> tuple[str, bool, str]:
    """Check role output shape before canonical materialization validation."""

    proposals_by_index = {
        proposal.output_index: proposal for proposal in result.materialization_proposals
    }
    if role is ProjectDeliverableRole.RUNNABLE_ENTRYPOINT:
        name = "runnable_entrypoint_deliverable"
        passed = any(
            artifact.artifact_type is EngineeringArtifactType.SOURCE
            and artifact.output_index in proposals_by_index
            for artifact in artifacts
        )
        detail = (
            "RUNNABLE_ENTRYPOINT has a canonical SOURCE artifact with "
            "materialization intent."
            if passed
            else "RUNNABLE_ENTRYPOINT task must produce a canonical SOURCE "
            "artifact covered by a materialization proposal."
        )
        return name, passed, detail
    if role is ProjectDeliverableRole.AUTOMATED_TESTS:
        name = "automated_tests_deliverable"
        passed = any(
            artifact.artifact_type is EngineeringArtifactType.TEST
            and artifact.output_index in proposals_by_index
            for artifact in artifacts
        )
        detail = (
            "AUTOMATED_TESTS has a canonical TEST artifact with materialization "
            "intent."
            if passed
            else "AUTOMATED_TESTS task must produce a canonical TEST artifact "
            "covered by a materialization proposal."
        )
        return name, passed, detail

    name = "run_instructions_deliverable"
    passed = any(
        artifact.artifact_type is EngineeringArtifactType.DOCUMENTATION
        and (proposal := proposals_by_index.get(artifact.output_index)) is not None
        and proposal.target_path == "README.md"
        for artifact in artifacts
    )
    detail = (
        "RUN_INSTRUCTIONS has a canonical DOCUMENTATION artifact targeting root "
        "README.md."
        if passed
        else "RUN_INSTRUCTIONS task must produce a canonical DOCUMENTATION "
        "artifact with a materialization proposal targeting root README.md."
    )
    return name, passed, detail


def classify_validation_failure(
    validation: TaskExecutionValidationResult,
) -> tuple[bool, str]:
    """Return intrinsic retryability and deterministic safe feedback."""

    if validation.passed:
        raise TaskExecutionContractError(
            "A passed validation result cannot be classified as a failure."
        )
    failed_checks = tuple(check for check in validation.checks if not check.passed)
    if not failed_checks:
        raise TaskExecutionContractError(
            "A failed validation result must identify at least one failed check."
        )
    retryable = all(
        check.name in RETRYABLE_VALIDATION_CHECKS for check in failed_checks
    )
    feedback = " ".join(check.detail for check in failed_checks)
    return retryable, feedback


def _derive_retry_context(
    *,
    requirement_spec: ApprovedRequirementSpec,
    task_graph: TaskGraph,
    task_state: TaskExecutionState,
    prior_recovery_decision: TaskExecutionRecoveryDecision | None,
) -> TaskExecutionRetryContext | None:
    current_attempt = task_state.attempt_count
    if current_attempt == 1:
        if prior_recovery_decision is not None:
            raise TaskExecutionContractError(
                "The first task attempt cannot have prior recovery evidence."
            )
        return None
    if prior_recovery_decision is None:
        raise TaskExecutionContractError(
            f"Task {task_state.task_id} attempt {current_attempt} requires its "
            "immediately prior RETRY recovery decision."
        )

    prior_attempt = current_attempt - 1
    expected_attempt_id = _attempt_id(
        task_graph.graph_id, task_state.task_id, prior_attempt
    )
    expected_request_id = _request_id(
        requirement_spec.spec_id, expected_attempt_id
    )
    mismatches: list[str] = []
    if prior_recovery_decision.task_id != task_state.task_id:
        mismatches.append("task_id")
    if prior_recovery_decision.attempt_number != prior_attempt:
        mismatches.append("attempt_number")
    if prior_recovery_decision.action is not TaskExecutionRecoveryAction.RETRY:
        mismatches.append("action")
    if not prior_recovery_decision.retryable:
        mismatches.append("retryable")
    if prior_recovery_decision.max_attempts != MAX_TASK_EXECUTION_ATTEMPTS:
        mismatches.append("max_attempts")
    if (
        prior_recovery_decision.failure_kind
        is TaskExecutionRecoveryFailureKind.REQUEST_BUILD
    ):
        mismatches.append("failure_kind")
    if prior_recovery_decision.attempt_id != expected_attempt_id:
        mismatches.append("attempt_id")
    if prior_recovery_decision.request_id != expected_request_id:
        mismatches.append("request_id")
    if not prior_recovery_decision.feedback.strip():
        mismatches.append("feedback")
    if mismatches:
        raise TaskExecutionContractError(
            "Prior recovery decision does not authorize this retry request: "
            + ", ".join(mismatches)
            + "."
        )
    return TaskExecutionRetryContext(
        prior_attempt_number=prior_attempt,
        prior_request_id=expected_request_id,
        prior_attempt_id=expected_attempt_id,
        failure_kind=prior_recovery_decision.failure_kind,
        feedback=prior_recovery_decision.feedback,
    )


def _validate_spec_and_execution(
    requirement_spec: ApprovedRequirementSpec,
    task_graph: TaskGraph,
    execution_state: TaskGraphExecutionState,
) -> None:
    if task_graph.requirement_spec_id != requirement_spec.spec_id or (
        task_graph.requirement_spec_version != requirement_spec.version
    ):
        raise TaskExecutionContractError(
            "TaskGraph does not belong to the supplied ApprovedRequirementSpec."
        )
    if execution_state.graph_id != task_graph.graph_id:
        raise TaskExecutionContractError(
            f"Execution graph {execution_state.graph_id} does not match "
            f"{task_graph.graph_id}."
        )
    expected_task_ids = tuple(task.task_id for task in task_graph.tasks)
    actual_task_ids = tuple(state.task_id for state in execution_state.task_states)
    if actual_task_ids != expected_task_ids:
        raise TaskExecutionContractError(
            "Execution task identities/order do not match the canonical TaskGraph."
        )


def _execution_states_by_id(
    execution_state: TaskGraphExecutionState,
) -> dict[str, TaskExecutionState]:
    return {state.task_id: state for state in execution_state.task_states}


def _resolve_requirement_context(
    spec: ApprovedRequirementSpec, task: Task
) -> TaskRequirementContext:
    requirement_items = {
        item.item_id: item
        for item in (
            *spec.functional_requirements,
            *spec.nonfunctional_requirements,
            *spec.constraints,
        )
    }
    acceptance_items = {item.item_id: item for item in spec.acceptance_criteria}
    risk_items = {item.item_id: item for item in spec.risks}
    ambiguity_items = {item.item_id: item for item in spec.ambiguities}
    _require_known_references("requirement", task.requirement_refs, requirement_items)
    _require_known_references(
        "acceptance-criteria", task.acceptance_criteria_refs, acceptance_items
    )
    _require_known_references("risk", task.risk_refs, risk_items)
    _require_known_references("ambiguity", task.ambiguity_refs, ambiguity_items)

    requirement_refs = set(task.requirement_refs)
    acceptance_refs = set(task.acceptance_criteria_refs)
    risk_refs = set(task.risk_refs)
    ambiguity_refs = set(task.ambiguity_refs)
    return TaskRequirementContext(
        normalized_problem_statement=spec.normalized_problem_statement,
        requirement_type=spec.requirement_type,
        assumptions=spec.assumptions,
        functional_requirements=tuple(
            item
            for item in spec.functional_requirements
            if item.item_id in requirement_refs
        ),
        nonfunctional_requirements=tuple(
            item
            for item in spec.nonfunctional_requirements
            if item.item_id in requirement_refs
        ),
        constraints=tuple(
            item for item in spec.constraints if item.item_id in requirement_refs
        ),
        acceptance_criteria=tuple(
            item
            for item in spec.acceptance_criteria
            if item.item_id in acceptance_refs
        ),
        risks=tuple(item for item in spec.risks if item.item_id in risk_refs),
        ambiguities=tuple(
            item for item in spec.ambiguities if item.item_id in ambiguity_refs
        ),
    )


def _require_known_references(
    label: str,
    references: tuple[str, ...],
    known_items: dict[str, RequirementSpecItem],
) -> None:
    missing = sorted(set(references) - set(known_items))
    if missing:
        raise TaskExecutionContractError(
            f"Task has unknown approved {label} references: "
            + ", ".join(missing)
            + "."
        )


def _validated_dependency_artifacts(
    spec: ApprovedRequirementSpec,
    graph: TaskGraph,
    states_by_id: dict[str, TaskExecutionState],
    task: Task,
    artifacts: tuple[EngineeringArtifact, ...],
    validations: tuple[TaskExecutionValidationResult, ...],
) -> tuple[EngineeringArtifact, ...]:
    artifact_ids = [artifact.artifact_id for artifact in artifacts]
    if len(artifact_ids) != len(set(artifact_ids)):
        raise TaskExecutionContractError(
            "Accepted dependency artifacts must have unique artifact IDs."
        )

    tasks_by_id = {candidate.task_id: candidate for candidate in graph.tasks}
    dependency_positions = {
        dependency_id: index for index, dependency_id in enumerate(task.depends_on)
    }
    artifacts_by_task: dict[str, list[EngineeringArtifact]] = {}
    for artifact in artifacts:
        if artifact.task_id not in dependency_positions:
            raise TaskExecutionContractError(
                f"Artifact {artifact.artifact_id} comes from {artifact.task_id}, "
                f"which is not a direct dependency of {task.task_id}."
            )
        dependency_state = states_by_id[artifact.task_id]
        if dependency_state.status is not TaskExecutionStatus.SUCCEEDED:
            raise TaskExecutionContractError(
                f"Artifact source task {artifact.task_id} is not SUCCEEDED."
            )
        if dependency_state.attempt_count < 1:
            raise TaskExecutionContractError(
                f"Artifact source task {artifact.task_id} has no accepted attempt."
            )
        expected_attempt_id = _attempt_id(
            graph.graph_id,
            artifact.task_id,
            dependency_state.attempt_count,
        )
        expected_request_id = _request_id(spec.spec_id, expected_attempt_id)
        if artifact.requirement_spec_id != spec.spec_id:
            raise TaskExecutionContractError(
                f"Artifact {artifact.artifact_id} belongs to another requirement spec."
            )
        if artifact.graph_id != graph.graph_id:
            raise TaskExecutionContractError(
                f"Artifact {artifact.artifact_id} belongs to another TaskGraph."
            )
        if (
            artifact.attempt_id != expected_attempt_id
            or artifact.attempt_number != dependency_state.attempt_count
            or artifact.request_id != expected_request_id
        ):
            raise TaskExecutionContractError(
                f"Artifact {artifact.artifact_id} is not from the accepted attempt "
                f"of {artifact.task_id}."
            )
        source_task = tasks_by_id[artifact.task_id]
        source_provenance = {
            "requirement_refs": source_task.requirement_refs,
            "acceptance_criteria_refs": source_task.acceptance_criteria_refs,
            "risk_refs": source_task.risk_refs,
            "ambiguity_refs": source_task.ambiguity_refs,
        }
        mismatched_source_fields = [
            field_name
            for field_name, expected_value in source_provenance.items()
            if getattr(artifact, field_name) != expected_value
        ]
        if not artifact.created_at.strip():
            mismatched_source_fields.append("created_at")
        if mismatched_source_fields:
            raise TaskExecutionContractError(
                f"Artifact {artifact.artifact_id} has invalid source provenance: "
                + ", ".join(mismatched_source_fields)
                + "."
            )
        identity_errors = _canonical_artifact_identity_errors(
            source_task, artifact
        )
        if identity_errors:
            raise TaskExecutionContractError(
                f"Artifact {artifact.artifact_id} is not canonical: "
                + "; ".join(identity_errors)
                + "."
            )
        artifacts_by_task.setdefault(artifact.task_id, []).append(artifact)

    validations_by_task: dict[str, TaskExecutionValidationResult] = {}
    for validation in validations:
        if validation.task_id in validations_by_task:
            raise TaskExecutionContractError(
                "Dependency validation evidence must be unique per source task: "
                f"{validation.task_id}."
            )
        validations_by_task[validation.task_id] = validation

    required_dependency_tasks = set(task.depends_on)
    validation_tasks = set(validations_by_task)
    missing_validations = sorted(required_dependency_tasks - validation_tasks)
    if missing_validations:
        raise TaskExecutionContractError(
            "Missing successful validation evidence for direct dependencies: "
            + ", ".join(missing_validations)
            + "."
        )
    extra_validations = sorted(validation_tasks - required_dependency_tasks)
    if extra_validations:
        raise TaskExecutionContractError(
            "Dependency validation evidence is not for a direct dependency: "
            + ", ".join(extra_validations)
            + "."
        )

    for source_task_id in task.depends_on:
        source_artifacts = artifacts_by_task.get(source_task_id, [])
        validation = validations_by_task[source_task_id]
        if not validation.passed:
            raise TaskExecutionContractError(
                f"Dependency artifacts from {source_task_id} did not pass validation."
            )
        dependency_state = states_by_id[source_task_id]
        if dependency_state.attempt_count < 1:
            raise TaskExecutionContractError(
                f"Artifact source task {source_task_id} has no accepted attempt."
            )
        expected_attempt_id = _attempt_id(
            graph.graph_id,
            source_task_id,
            dependency_state.attempt_count,
        )
        expected_request_id = _request_id(spec.spec_id, expected_attempt_id)
        correlation_mismatches = []
        if validation.request_id != expected_request_id:
            correlation_mismatches.append("request_id")
        if validation.attempt_id != expected_attempt_id:
            correlation_mismatches.append("attempt_id")
        if validation.task_id != source_task_id:
            correlation_mismatches.append("task_id")
        if correlation_mismatches:
            raise TaskExecutionContractError(
                f"Dependency validation for {source_task_id} has mismatched "
                "correlation: " + ", ".join(correlation_mismatches) + "."
            )

        canonical_source_artifacts = tuple(
            sorted(
                source_artifacts,
                key=lambda artifact: (artifact.output_index, artifact.artifact_id),
            )
        )
        supplied_artifact_ids = tuple(
            artifact.artifact_id for artifact in canonical_source_artifacts
        )
        if supplied_artifact_ids != validation.artifact_ids:
            raise TaskExecutionContractError(
                f"Dependency artifact set for {source_task_id} does not exactly "
                "match its successful validation evidence."
            )

    return tuple(
        sorted(
            artifacts,
            key=lambda artifact: (
                dependency_positions[artifact.task_id],
                artifact.output_index,
                artifact.artifact_id,
            ),
        )
    )


def _correlation_mismatches(
    request: TaskExecutionRequest, result: TaskExecutionResult
) -> tuple[str, ...]:
    mismatches = []
    if result.request_id != request.request_id:
        mismatches.append("request_id")
    if result.attempt_id != request.attempt_id:
        mismatches.append("attempt_id")
    if result.task_id != request.task_id:
        mismatches.append("task_id")
    return tuple(mismatches)


def _artifact_provenance_errors(
    request: TaskExecutionRequest,
    artifacts: tuple[EngineeringArtifact, ...],
) -> tuple[str, ...]:
    expected = {
        "requirement_spec_id": request.requirement_spec_id,
        "graph_id": request.graph_id,
        "task_id": request.task_id,
        "request_id": request.request_id,
        "attempt_id": request.attempt_id,
        "attempt_number": request.attempt_number,
        "requirement_refs": request.task.requirement_refs,
        "acceptance_criteria_refs": request.task.acceptance_criteria_refs,
        "risk_refs": request.task.risk_refs,
        "ambiguity_refs": request.task.ambiguity_refs,
    }
    errors: list[str] = []
    for position, artifact in enumerate(artifacts, start=1):
        mismatched_fields = [
            field_name
            for field_name, expected_value in expected.items()
            if getattr(artifact, field_name) != expected_value
        ]
        if not artifact.created_at.strip():
            mismatched_fields.append("created_at")
        if mismatched_fields:
            errors.append(
                f"output {position} fields " + ", ".join(mismatched_fields)
            )
    return tuple(errors)


def _artifact_identity_errors(
    request: TaskExecutionRequest,
    artifacts: tuple[EngineeringArtifact, ...],
) -> tuple[str, ...]:
    errors: list[str] = []
    for position, artifact in enumerate(artifacts, start=1):
        artifact_errors = _canonical_artifact_identity_errors(
            request.task, artifact
        )
        if artifact_errors:
            errors.append(
                f"output {position} " + ", ".join(artifact_errors)
            )
    return tuple(errors)


def _canonical_artifact_identity_errors(
    task: Task, artifact: EngineeringArtifact
) -> tuple[str, ...]:
    output = ArtifactOutput(
        artifact_type=artifact.artifact_type,
        logical_name=artifact.logical_name,
        content=artifact.content,
    )
    expected_hash = _artifact_content_hash_from_values(
        requirement_spec_id=artifact.requirement_spec_id,
        graph_id=artifact.graph_id,
        task_id=artifact.task_id,
        request_id=artifact.request_id,
        attempt_id=artifact.attempt_id,
        attempt_number=artifact.attempt_number,
        requirement_refs=artifact.requirement_refs,
        acceptance_criteria_refs=artifact.acceptance_criteria_refs,
        risk_refs=artifact.risk_refs,
        ambiguity_refs=artifact.ambiguity_refs,
        output=output,
        output_index=artifact.output_index,
    )
    errors: list[str] = []
    if artifact.content_hash != expected_hash:
        errors.append("content_hash")
    if artifact.artifact_id != _artifact_id(expected_hash, artifact.output_index):
        errors.append("artifact_id")
    if artifact.lineage_id != _artifact_lineage_id(
        task, output, artifact.output_index
    ):
        errors.append("lineage_id")
    return tuple(errors)


def _artifact_correspondence_errors(
    result: TaskExecutionResult,
    artifacts: tuple[EngineeringArtifact, ...],
) -> tuple[str, ...]:
    errors: list[str] = []
    for position, (output, artifact) in enumerate(
        zip(result.outputs, artifacts, strict=False), start=1
    ):
        if artifact.output_index != position:
            errors.append(f"output {position} index")
        if artifact.artifact_type is not output.artifact_type:
            errors.append(f"output {position} artifact_type")
        if artifact.logical_name != output.logical_name:
            errors.append(f"output {position} logical_name")
        if artifact.content != output.content:
            errors.append(f"output {position} content")
    if len(result.outputs) != len(artifacts):
        errors.append("output/artifact collection length")
    return tuple(errors)


def _attempt_id(graph_id: str, task_id: str, attempt_number: int) -> str:
    return str(
        uuid5(
            LINEAGE_NAMESPACE,
            f"task-execution-attempt:{graph_id}:{task_id}:{attempt_number}",
        )
    )


def _request_id(requirement_spec_id: str, attempt_id: str) -> str:
    return str(
        uuid5(
            LINEAGE_NAMESPACE,
            f"task-execution-request:{requirement_spec_id}:{attempt_id}",
        )
    )


def _artifact_lineage_id(
    task: Task, output: ArtifactOutput, output_index: int
) -> str:
    return str(
        uuid5(
            LINEAGE_NAMESPACE,
            "engineering-artifact-lineage:"
            f"{task.lineage_id}:{output_index}:{output.artifact_type.value}:"
            f"{output.logical_name}",
        )
    )


def _artifact_id(content_hash: str, output_index: int) -> str:
    return f"ARTIFACT-{content_hash[:12].upper()}-O{output_index:03d}"


def _artifact_content_hash(
    *,
    request: TaskExecutionRequest,
    output: ArtifactOutput,
    output_index: int,
) -> str:
    return _artifact_content_hash_from_values(
        requirement_spec_id=request.requirement_spec_id,
        graph_id=request.graph_id,
        task_id=request.task_id,
        request_id=request.request_id,
        attempt_id=request.attempt_id,
        attempt_number=request.attempt_number,
        requirement_refs=request.task.requirement_refs,
        acceptance_criteria_refs=request.task.acceptance_criteria_refs,
        risk_refs=request.task.risk_refs,
        ambiguity_refs=request.task.ambiguity_refs,
        output=output,
        output_index=output_index,
    )


def _artifact_content_hash_from_values(
    *,
    requirement_spec_id: str,
    graph_id: str,
    task_id: str,
    request_id: str,
    attempt_id: str,
    attempt_number: int,
    requirement_refs: tuple[str, ...],
    acceptance_criteria_refs: tuple[str, ...],
    risk_refs: tuple[str, ...],
    ambiguity_refs: tuple[str, ...],
    output: ArtifactOutput,
    output_index: int,
) -> str:
    return _content_hash(
        {
            "requirement_spec_id": requirement_spec_id,
            "graph_id": graph_id,
            "task_id": task_id,
            "request_id": request_id,
            "attempt_id": attempt_id,
            "attempt_number": attempt_number,
            "requirement_refs": requirement_refs,
            "acceptance_criteria_refs": acceptance_criteria_refs,
            "risk_refs": risk_refs,
            "ambiguity_refs": ambiguity_refs,
            "output_index": output_index,
            "artifact_type": output.artifact_type.value,
            "logical_name": output.logical_name,
            "content": output.content,
        }
    )


def _content_hash(value: object) -> str:
    canonical_json = json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )
    return hashlib.sha256(canonical_json.encode("utf-8")).hexdigest()
