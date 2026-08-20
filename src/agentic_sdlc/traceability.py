"""Read-only requirement-to-code traceability over retained workflow state."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, TypeVar

from pydantic import BaseModel, ConfigDict

from agentic_sdlc.brownfield_baseline import BrownfieldBaselineProvenance
from agentic_sdlc.brownfield_context import BrownfieldCodebaseContext
from agentic_sdlc.project_export import ProjectExportResult
from agentic_sdlc.project_readiness import (
    ProjectReadinessValidation,
)
from agentic_sdlc.requirement_spec import (
    ApprovedRequirementSpec,
    RequirementSpecItem,
)
from agentic_sdlc.task_execution import (
    TaskExecutionStatus,
    TaskGraphExecutionState,
    TaskGraphExecutionStatus,
    validate_task_graph_source_authority,
)
from agentic_sdlc.task_execution_contracts import (
    EngineeringArtifact,
    TaskExecutionRequest,
    TaskExecutionResult,
    TaskExecutionValidationResult,
    validate_execution_result,
)
from agentic_sdlc.task_graph import (
    Task,
    TaskGraph,
    ValidationExecutionProfile,
    derive_task_graph_semantics,
)
from agentic_sdlc.validation_execution_contracts import (
    TaskValidationExecutionEvidence,
    TaskValidationProvisioningEvidence,
    verified_required_validation_execution_evidence,
)
from agentic_sdlc.workspace_contracts import (
    ArtifactMaterializationValidationResult,
    WorkspaceChangeOperation,
    WorkspaceChangeSet,
    WorkspaceChangeSetValidationResult,
    WorkspaceSnapshot,
    artifact_materialization_validation_identity_is_valid,
    workspace_change_set_identity_is_valid,
    workspace_file_content_hash,
    workspace_snapshot_identity_is_valid,
)
from agentic_sdlc.workspace_integration_contracts import (
    TaskAttemptExitDecision,
    TaskAttemptExitDisposition,
    WorkspaceBoundTaskExecutionRequest,
)
from agentic_sdlc.workspace_mutation import (
    WorkspaceMutationResult,
    WorkspaceMutationStatus,
    workspace_mutation_result_identity_is_valid,
)


class TraceabilityProjectionError(ValueError):
    """Raised when no canonical approved requirement authority can be projected."""


class TraceabilityItemKind(StrEnum):
    """Canonical approved item namespaces included in Slice 1."""

    FUNCTIONAL_REQUIREMENT = "FR"
    NONFUNCTIONAL_REQUIREMENT = "NFR"
    CONSTRAINT = "CON"
    ACCEPTANCE_CRITERION = "AC"


class TraceabilityStatus(StrEnum):
    """Conservative presentation status derived from authoritative evidence."""

    VERIFIED = "VERIFIED"
    UNVERIFIED = "UNVERIFIED"
    NOT_IMPLEMENTED = "NOT_IMPLEMENTED"


def traceability_status_heading(status: TraceabilityStatus) -> str:
    """Return plain-English presentation text without changing derived status."""

    return {
        TraceabilityStatus.VERIFIED: (
            "VERIFIED — Implementation and validation are both traceable"
        ),
        TraceabilityStatus.UNVERIFIED: (
            "UNVERIFIED — Implemented, validation not proven"
        ),
        TraceabilityStatus.NOT_IMPLEMENTED: (
            "NOT_IMPLEMENTED — No implementation outcome is traceable"
        ),
    }[status]


def traceability_status_explanation(status: TraceabilityStatus) -> str:
    """Explain one derived status for readers without strengthening evidence."""

    return {
        TraceabilityStatus.VERIFIED: (
            "Implemented and explicitly linked to successful governed validation."
        ),
        TraceabilityStatus.UNVERIFIED: (
            "Implemented, but successful validation cannot be explicitly traced "
            "to this item. This does not mean implementation or tests failed."
        ),
        TraceabilityStatus.NOT_IMPLEMENTED: (
            "No authoritative implementation outcome is traceable to this item."
        ),
    }[status]


class TraceabilityRelationshipBasis(StrEnum):
    """Whether a relationship was recorded directly or joined deterministically."""

    EXPLICIT = "EXPLICIT"
    DERIVED = "DERIVED"


class TraceabilityGapCode(StrEnum):
    """Stable categories for intentionally visible projection gaps."""

    TASK_GRAPH_AUTHORITY = "TASK_GRAPH_AUTHORITY"
    TASK_LINK = "TASK_LINK"
    FINAL_TASK_AUTHORITY = "FINAL_TASK_AUTHORITY"
    IMPLEMENTATION_LINEAGE = "IMPLEMENTATION_LINEAGE"
    GOVERNED_VALIDATION = "GOVERNED_VALIDATION"
    FINAL_RUN_AUTHORITY = "FINAL_RUN_AUTHORITY"
    STATE_EVIDENCE = "STATE_EVIDENCE"
    BROWNFIELD_CORRELATION = "BROWNFIELD_CORRELATION"
    PUBLICATION = "PUBLICATION"


class TraceabilityAuthorityLink(BaseModel):
    """Exact canonical spec authority for one projected item."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    spec_id: str
    spec_version: int
    source_analysis_revision: int
    item_lineage_id: str
    basis: TraceabilityRelationshipBasis = TraceabilityRelationshipBasis.EXPLICIT


class TraceabilityTaskLink(BaseModel):
    """Approved TaskGraph task that explicitly references one canonical item."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    task_id: str
    title: str
    basis: TraceabilityRelationshipBasis = TraceabilityRelationshipBasis.EXPLICIT


class TraceabilityArtifactLink(BaseModel):
    """Canonical final-attempt artifact identity, distinct from any target path."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    artifact_id: str
    artifact_type: str
    logical_name: str
    task_id: str
    request_id: str
    attempt_id: str
    basis: TraceabilityRelationshipBasis = TraceabilityRelationshipBasis.DERIVED


class TraceabilityImplementationLink(BaseModel):
    """Validated materialization and applied mutation for one repository path."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    artifact_id: str
    task_id: str
    target_path: str
    operation: WorkspaceChangeOperation
    materialization_validation_id: str
    change_set_id: str
    mutation_id: str
    expected_preimage_hash: str | None
    observed_postimage_hash: str
    basis: TraceabilityRelationshipBasis = TraceabilityRelationshipBasis.DERIVED


class TraceabilityValidationLink(BaseModel):
    """Exact governed PASS evidence for an approved task validation requirement."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    task_id: str
    validation_requirement_id: str
    profile: ValidationExecutionProfile
    outcome: str
    evidence_id: str
    policy_id: str
    policy_version: str
    provisioning_evidence_ids: tuple[str, ...]
    basis: TraceabilityRelationshipBasis = TraceabilityRelationshipBasis.DERIVED


class TraceabilityEvidenceLink(BaseModel):
    """Named immutable evidence record reachable through one row."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    evidence_id: str
    evidence_kind: str
    task_id: str | None
    basis: TraceabilityRelationshipBasis = TraceabilityRelationshipBasis.DERIVED


class TraceabilityGap(BaseModel):
    """One deterministic reason a relationship or status cannot be established."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    code: TraceabilityGapCode
    detail: str


class TraceabilityRow(BaseModel):
    """Reader-facing projection for one canonical approved item."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    item_id: str
    item_kind: TraceabilityItemKind
    text: str
    authority_links: tuple[TraceabilityAuthorityLink, ...]
    task_links: tuple[TraceabilityTaskLink, ...]
    artifact_links: tuple[TraceabilityArtifactLink, ...]
    implementation_links: tuple[TraceabilityImplementationLink, ...]
    validation_links: tuple[TraceabilityValidationLink, ...]
    evidence_links: tuple[TraceabilityEvidenceLink, ...]
    status: TraceabilityStatus
    status_reason: str
    gaps: tuple[TraceabilityGap, ...]


def traceability_row_evaluator_reason(row: TraceabilityRow) -> str:
    """Summarize one row conservatively in plain human-readable language."""

    if row.status is TraceabilityStatus.VERIFIED:
        targets = ", ".join(link.target_path for link in row.implementation_links)
        profiles = ", ".join(
            dict.fromkeys(link.profile.value for link in row.validation_links)
        )
        return (
            f"{targets} and successful governed {profiles} validation are both "
            "explicitly traceable through covering tasks."
        )
    if row.status is TraceabilityStatus.UNVERIFIED:
        if row.implementation_links and not row.validation_links:
            targets = ", ".join(
                link.target_path for link in row.implementation_links
            )
            verb = "has" if len(row.implementation_links) == 1 else "have"
            return (
                f"{targets} {verb} an authoritative implementation outcome, but "
                "successful governed validation is not explicitly linked to this "
                "item."
            )
        return (
            "Implementation and validation links exist, but the complete governed "
            "run-authority chain cannot be established."
        )
    return (
        "No authoritative file change or NO_CHANGE outcome is traceable through "
        "a covering successful task."
    )


class TraceabilityFinalAuthority(BaseModel):
    """Run-level final workspace/readiness/publication facts for row details."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    workflow_status: str
    exit_gate_passed: bool
    readiness_validation_id: str | None
    readiness_passed: bool
    final_workspace_snapshot_id: str | None
    publication_project_name: str | None
    publication_succeeded: bool


class BrownfieldLineageStep(BaseModel):
    """One actual run-level brownfield authority or deterministic join."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    stage: str
    identity: str
    detail: str
    basis: TraceabilityRelationshipBasis


class BrownfieldTraceabilityLineage(BaseModel):
    """Fail-closed brownfield lineage available at projection time."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    verified: bool
    steps: tuple[BrownfieldLineageStep, ...]
    gaps: tuple[TraceabilityGap, ...]


class RequirementTraceabilityProjection(BaseModel):
    """Read-only deterministic interpretation of existing governed authority."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    run_id: str
    requirement_spec_id: str
    requirement_spec_version: int
    source_analysis_revision: int
    task_graph_id: str | None
    task_graph_version: int | None
    rows: tuple[TraceabilityRow, ...]
    final_authority: TraceabilityFinalAuthority
    brownfield_lineage: BrownfieldTraceabilityLineage | None


@dataclass(frozen=True, slots=True)
class _FinalTaskAttempt:
    task: Task
    request: TaskExecutionRequest
    result: TaskExecutionResult
    validation: TaskExecutionValidationResult
    artifacts: tuple[EngineeringArtifact, ...]
    exit_decision: TaskAttemptExitDecision


@dataclass(frozen=True, slots=True)
class _EvidenceState:
    execution: TaskGraphExecutionState | None
    requests: tuple[TaskExecutionRequest, ...]
    results: tuple[TaskExecutionResult, ...]
    execution_validations: tuple[TaskExecutionValidationResult, ...]
    artifacts: tuple[EngineeringArtifact, ...]
    materialization_validations: tuple[ArtifactMaterializationValidationResult, ...]
    change_sets: tuple[WorkspaceChangeSet, ...]
    change_set_validations: tuple[WorkspaceChangeSetValidationResult, ...]
    mutations: tuple[WorkspaceMutationResult, ...]
    snapshots: tuple[WorkspaceSnapshot, ...]
    bound_requests: tuple[WorkspaceBoundTaskExecutionRequest, ...]
    validation_evidence: tuple[TaskValidationExecutionEvidence, ...]
    provisioning_evidence: tuple[TaskValidationProvisioningEvidence, ...]
    exit_decisions: tuple[TaskAttemptExitDecision, ...]
    load_gaps: tuple[str, ...]


_ModelT = TypeVar("_ModelT", bound=BaseModel)


def build_requirement_traceability(
    state: Mapping[str, Any],
    *,
    export_result: ProjectExportResult | None = None,
) -> RequirementTraceabilityProjection:
    """Project canonical items through exact retained governed evidence relationships.

    The function performs no I/O, execution, mutation, approval, persistence, or
    publication.  Missing or inconsistent joins remain visible and fail closed.
    """

    spec = _required_model(
        state.get("approved_requirement_spec"),
        ApprovedRequirementSpec,
        "approved requirement specification",
    )
    items = _canonical_items(spec)
    run_id = str(state.get("run_id", ""))
    graph, graph_gap = _approved_graph(state, spec)
    evidence = _evidence_state(state)
    final_authority, final_authority_complete = _final_authority(
        state,
        evidence,
        export_result,
    )
    graph_tasks = graph.tasks if graph is not None else ()
    final_attempts = {
        task.task_id: attempt
        for task in graph_tasks
        if (
            attempt := _final_task_attempt(
                spec,
                graph,
                task,
                evidence,
            )
        )
        is not None
    }
    verified_validation = verified_required_validation_execution_evidence(
        graph,
        evidence.execution,
        run_id=run_id or None,
        bound_requests=evidence.bound_requests,
        evidence=evidence.validation_evidence,
        provisioning_evidence=evidence.provisioning_evidence,
        snapshots=evidence.snapshots,
        change_sets=evidence.change_sets,
        exit_decisions=evidence.exit_decisions,
    )
    rows = tuple(
        _row(
            kind,
            item,
            spec=spec,
            graph=graph,
            graph_gap=graph_gap,
            evidence=evidence,
            final_attempts=final_attempts,
            verified_validation=verified_validation,
            final_authority_complete=final_authority_complete,
        )
        for kind, item in items
    )
    return RequirementTraceabilityProjection(
        run_id=run_id,
        requirement_spec_id=spec.spec_id,
        requirement_spec_version=spec.version,
        source_analysis_revision=spec.source_analysis_revision,
        task_graph_id=graph.graph_id if graph is not None else None,
        task_graph_version=graph.version if graph is not None else None,
        rows=rows,
        final_authority=final_authority,
        brownfield_lineage=_brownfield_lineage(
            state,
            spec=spec,
            graph=graph,
            evidence=evidence,
            final_authority=final_authority,
            final_authority_complete=final_authority_complete,
            export_result=export_result,
        ),
    )


def _canonical_items(
    spec: ApprovedRequirementSpec,
) -> tuple[tuple[TraceabilityItemKind, RequirementSpecItem], ...]:
    items = tuple(
        (kind, item)
        for kind, group in (
            (TraceabilityItemKind.FUNCTIONAL_REQUIREMENT, spec.functional_requirements),
            (
                TraceabilityItemKind.NONFUNCTIONAL_REQUIREMENT,
                spec.nonfunctional_requirements,
            ),
            (TraceabilityItemKind.CONSTRAINT, spec.constraints),
            (TraceabilityItemKind.ACCEPTANCE_CRITERION, spec.acceptance_criteria),
        )
        for item in group
    )
    item_ids = tuple(item.item_id for _, item in items)
    if len(item_ids) != len(set(item_ids)):
        raise TraceabilityProjectionError(
            "Canonical approved traceability item IDs are not unique."
        )
    return items


def _approved_graph(
    state: Mapping[str, Any],
    spec: ApprovedRequirementSpec,
) -> tuple[TaskGraph | None, str | None]:
    value = state.get("approved_task_graph")
    if value is None:
        return None, "No approved TaskGraph is present."
    try:
        graph = _required_model(value, TaskGraph, "approved TaskGraph")
        validate_task_graph_source_authority(graph, spec)
        derive_task_graph_semantics(graph.tasks)
    except (ValueError, TypeError) as error:
        return None, f"Approved TaskGraph authority is invalid: {error}"
    if state.get("task_graph_decision") != "APPROVE":
        return None, "The TaskGraph lacks the authoritative APPROVE decision."
    return graph, None


def _evidence_state(state: Mapping[str, Any]) -> _EvidenceState:
    gaps: list[str] = []
    execution = _optional_model(
        state.get("task_graph_execution"),
        TaskGraphExecutionState,
        "task_graph_execution",
        gaps,
    )
    return _EvidenceState(
        execution=execution,
        requests=_model_sequence(state, "task_execution_requests", TaskExecutionRequest, gaps),
        results=_model_sequence(state, "task_execution_results", TaskExecutionResult, gaps),
        execution_validations=_model_sequence(
            state,
            "task_execution_validations",
            TaskExecutionValidationResult,
            gaps,
        ),
        artifacts=_model_sequence(state, "engineering_artifacts", EngineeringArtifact, gaps),
        materialization_validations=_model_sequence(
            state,
            "artifact_materialization_validations",
            ArtifactMaterializationValidationResult,
            gaps,
        ),
        change_sets=_model_sequence(state, "workspace_change_sets", WorkspaceChangeSet, gaps),
        change_set_validations=_model_sequence(
            state,
            "workspace_change_set_validations",
            WorkspaceChangeSetValidationResult,
            gaps,
        ),
        mutations=_model_sequence(
            state,
            "workspace_mutation_results",
            WorkspaceMutationResult,
            gaps,
        ),
        snapshots=_model_sequence(state, "workspace_snapshots", WorkspaceSnapshot, gaps),
        bound_requests=_model_sequence(
            state,
            "workspace_bound_task_execution_requests",
            WorkspaceBoundTaskExecutionRequest,
            gaps,
        ),
        validation_evidence=_model_sequence(
            state,
            "task_validation_execution_evidence",
            TaskValidationExecutionEvidence,
            gaps,
        ),
        provisioning_evidence=_model_sequence(
            state,
            "task_validation_provisioning_evidence",
            TaskValidationProvisioningEvidence,
            gaps,
        ),
        exit_decisions=_model_sequence(
            state,
            "task_attempt_exit_decisions",
            TaskAttemptExitDecision,
            gaps,
        ),
        load_gaps=tuple(gaps),
    )


def _final_task_attempt(
    spec: ApprovedRequirementSpec,
    graph: TaskGraph,
    task: Task,
    evidence: _EvidenceState,
) -> _FinalTaskAttempt | None:
    execution = evidence.execution
    if execution is None or execution.graph_id != graph.graph_id:
        return None
    runtime = next(
        (item for item in execution.task_states if item.task_id == task.task_id),
        None,
    )
    if (
        runtime is None
        or runtime.status is not TaskExecutionStatus.SUCCEEDED
        or runtime.attempt_count < 1
    ):
        return None
    requests = tuple(
        item
        for item in evidence.requests
        if item.task_id == task.task_id
        and item.attempt_number == runtime.attempt_count
    )
    if len(requests) != 1:
        return None
    request = requests[0]
    if (
        request.graph_id != graph.graph_id
        or request.requirement_spec_id != spec.spec_id
        or request.task != task
        or request.attempt_id == ""
        or request.request_id == ""
    ):
        return None
    results = tuple(
        item
        for item in evidence.results
        if item.task_id == task.task_id
        and item.request_id == request.request_id
        and item.attempt_id == request.attempt_id
    )
    if len(results) != 1:
        return None
    result = results[0]
    artifacts = tuple(
        sorted(
            (
                item
                for item in evidence.artifacts
                if item.task_id == task.task_id
                and item.request_id == request.request_id
                and item.attempt_id == request.attempt_id
                and item.attempt_number == runtime.attempt_count
            ),
            key=lambda item: (item.output_index, item.artifact_id),
        )
    )
    validations = tuple(
        item
        for item in evidence.execution_validations
        if item.task_id == task.task_id
        and item.request_id == request.request_id
        and item.attempt_id == request.attempt_id
    )
    if len(validations) != 1:
        return None
    try:
        expected_validation = validate_execution_result(request, result, artifacts)
    except (ValueError, TypeError):
        return None
    validation = validations[0]
    if validation != expected_validation or not validation.passed:
        return None
    exits = tuple(
        item
        for item in evidence.exit_decisions
        if item.task_id == task.task_id
        and item.attempt_number == runtime.attempt_count
        and item.request_id == request.request_id
        and item.attempt_id == request.attempt_id
        and item.disposition is TaskAttemptExitDisposition.SUCCEED_TASK
    )
    if len(exits) != 1:
        return None
    return _FinalTaskAttempt(
        task=task,
        request=request,
        result=result,
        validation=validation,
        artifacts=artifacts,
        exit_decision=exits[0],
    )


def _row(
    kind: TraceabilityItemKind,
    item: RequirementSpecItem,
    *,
    spec: ApprovedRequirementSpec,
    graph: TaskGraph | None,
    graph_gap: str | None,
    evidence: _EvidenceState,
    final_attempts: Mapping[str, _FinalTaskAttempt],
    verified_validation: tuple[TaskValidationExecutionEvidence, ...],
    final_authority_complete: bool,
) -> TraceabilityRow:
    graph_tasks = graph.tasks if graph is not None else ()
    tasks = tuple(
        task
        for task in graph_tasks
        if item.item_id
        in (
            task.acceptance_criteria_refs
            if kind is TraceabilityItemKind.ACCEPTANCE_CRITERION
            else task.requirement_refs
        )
    )
    attempts = tuple(
        final_attempts[task.task_id]
        for task in tasks
        if task.task_id in final_attempts
    )
    artifact_links = tuple(
        TraceabilityArtifactLink(
            artifact_id=artifact.artifact_id,
            artifact_type=artifact.artifact_type.value,
            logical_name=artifact.logical_name,
            task_id=attempt.task.task_id,
            request_id=attempt.request.request_id,
            attempt_id=attempt.request.attempt_id,
        )
        for attempt in attempts
        for artifact in attempt.artifacts
    )
    implementation_links: list[TraceabilityImplementationLink] = []
    evidence_links: list[TraceabilityEvidenceLink] = []
    incomplete_implementation_tasks: list[str] = []
    for attempt in attempts:
        links, retained, incomplete = _implementation_lineage(attempt, evidence)
        implementation_links.extend(links)
        evidence_links.extend(retained)
        if incomplete:
            incomplete_implementation_tasks.append(attempt.task.task_id)
    covering_ids = frozenset(task.task_id for task in tasks)
    validation_records = tuple(
        record
        for record in verified_validation
        if record.task_id in covering_ids
    )
    validation_links = tuple(
        TraceabilityValidationLink(
            task_id=record.task_id,
            validation_requirement_id=record.validation_requirement_id,
            profile=record.profile,
            outcome=record.outcome.value,
            evidence_id=record.evidence_id,
            policy_id=record.policy_id,
            policy_version=record.policy_version,
            provisioning_evidence_ids=record.provisioning_evidence_ids,
        )
        for record in validation_records
    )
    for record in validation_records:
        evidence_links.append(
            TraceabilityEvidenceLink(
                evidence_id=record.evidence_id,
                evidence_kind="GOVERNED_VALIDATION_EXECUTION",
                task_id=record.task_id,
            )
        )
        evidence_links.extend(
            TraceabilityEvidenceLink(
                evidence_id=evidence_id,
                evidence_kind="VALIDATION_PROVISIONING",
                task_id=record.task_id,
            )
            for evidence_id in record.provisioning_evidence_ids
        )

    gaps: list[TraceabilityGap] = []
    if graph_gap is not None:
        gaps.append(
            TraceabilityGap(
                code=TraceabilityGapCode.TASK_GRAPH_AUTHORITY,
                detail=graph_gap,
            )
        )
    if not tasks:
        gaps.append(
            TraceabilityGap(
                code=TraceabilityGapCode.TASK_LINK,
                detail="No approved TaskGraph task explicitly references this item.",
            )
        )
    missing_final = tuple(
        task.task_id for task in tasks if task.task_id not in final_attempts
    )
    if missing_final:
        gaps.append(
            TraceabilityGap(
                code=TraceabilityGapCode.FINAL_TASK_AUTHORITY,
                detail=(
                    "No exact successful final-attempt authority is established for: "
                    + ", ".join(missing_final)
                    + "."
                ),
            )
        )
    if incomplete_implementation_tasks:
        gaps.append(
            TraceabilityGap(
                code=TraceabilityGapCode.IMPLEMENTATION_LINEAGE,
                detail=(
                    "Final-attempt materialization evidence is incomplete or "
                    "mismatched for: "
                    + ", ".join(incomplete_implementation_tasks)
                    + "."
                ),
            )
        )
    if not implementation_links:
        gaps.append(
            TraceabilityGap(
                code=TraceabilityGapCode.IMPLEMENTATION_LINEAGE,
                detail=(
                    "No final-authority materialized implementation target is "
                    "traceable through covering successful tasks."
                ),
            )
        )
    if implementation_links and not validation_links:
        gaps.append(
            TraceabilityGap(
                code=TraceabilityGapCode.GOVERNED_VALIDATION,
                detail=(
                    "No qualifying governed PASS validation evidence is traceable "
                    "through covering tasks."
                ),
            )
        )
    if implementation_links and validation_links and not final_authority_complete:
        gaps.append(
            TraceabilityGap(
                code=TraceabilityGapCode.FINAL_RUN_AUTHORITY,
                detail=(
                    "Workflow success, exit-gate, readiness, and final-snapshot "
                    "authority are not all established."
                ),
            )
        )
    gaps.extend(
        TraceabilityGap(code=TraceabilityGapCode.STATE_EVIDENCE, detail=detail)
        for detail in evidence.load_gaps
    )
    if not implementation_links:
        status = TraceabilityStatus.NOT_IMPLEMENTED
        reason = (
            "No final-authority materialized implementation target is traceable "
            "through covering successful tasks."
        )
    elif not validation_links:
        status = TraceabilityStatus.UNVERIFIED
        target = implementation_links[0]
        reason = (
            f"Implementation target {target.target_path} reached a final-authority "
            f"{target.operation.value} outcome, but no qualifying governed validation "
            "evidence is traceable to this item."
        )
    elif not final_authority_complete:
        status = TraceabilityStatus.UNVERIFIED
        reason = (
            "Implementation and governed validation lineage exist, but final "
            "workflow/readiness authority is incomplete."
        )
    else:
        status = TraceabilityStatus.VERIFIED
        profiles = ", ".join(
            dict.fromkeys(link.profile.value for link in validation_links)
        )
        reason = (
            "Materialized implementation and exact governed "
            f"{profiles} PASS evidence are both traceable through covering tasks."
        )
    return TraceabilityRow(
        item_id=item.item_id,
        item_kind=kind,
        text=item.text,
        authority_links=(
            TraceabilityAuthorityLink(
                spec_id=spec.spec_id,
                spec_version=spec.version,
                source_analysis_revision=spec.source_analysis_revision,
                item_lineage_id=item.lineage_id,
            ),
        ),
        task_links=tuple(
            TraceabilityTaskLink(task_id=task.task_id, title=task.title)
            for task in tasks
        ),
        artifact_links=artifact_links,
        implementation_links=tuple(implementation_links),
        validation_links=validation_links,
        evidence_links=_unique_evidence_links(evidence_links),
        status=status,
        status_reason=reason,
        gaps=_unique_gaps(gaps),
    )


def _implementation_lineage(
    attempt: _FinalTaskAttempt,
    evidence: _EvidenceState,
) -> tuple[
    tuple[TraceabilityImplementationLink, ...],
    tuple[TraceabilityEvidenceLink, ...],
    bool,
]:
    materializations = tuple(
        item
        for item in evidence.materialization_validations
        if item.task_id == attempt.task.task_id
        and item.request_id == attempt.request.request_id
        and item.attempt_id == attempt.request.attempt_id
    )
    if len(materializations) != 1:
        return (), (), bool(materializations)
    materialization = materializations[0]
    if (
        not materialization.passed
        or not artifact_materialization_validation_identity_is_valid(materialization)
        or materialization.artifact_ids
        != tuple(item.artifact_id for item in attempt.artifacts)
        or materialization.materialization_validation_id
        not in attempt.exit_decision.evidence_ids
    ):
        return (), (), True
    retained = [
        TraceabilityEvidenceLink(
            evidence_id=materialization.materialization_validation_id,
            evidence_kind="MATERIALIZATION_VALIDATION",
            task_id=attempt.task.task_id,
        )
    ]
    if not materialization.intents:
        return (), tuple(retained), False
    change_sets = tuple(
        item
        for item in evidence.change_sets
        if item.materialization_validation_id
        == materialization.materialization_validation_id
    )
    if len(change_sets) != 1:
        return (), tuple(retained), True
    change_set = change_sets[0]
    if (
        not workspace_change_set_identity_is_valid(change_set)
        or (
            change_set.requirement_spec_id,
            change_set.graph_id,
            change_set.task_id,
            change_set.request_id,
            change_set.attempt_id,
            change_set.attempt_number,
        )
        != (
            attempt.request.requirement_spec_id,
            attempt.request.graph_id,
            attempt.request.task_id,
            attempt.request.request_id,
            attempt.request.attempt_id,
            attempt.request.attempt_number,
        )
        or change_set.change_set_id not in attempt.exit_decision.evidence_ids
    ):
        return (), tuple(retained), True
    base_snapshot = next(
        (
            item
            for item in evidence.snapshots
            if item.snapshot_id == change_set.base_snapshot_id
        ),
        None,
    )
    change_validations = tuple(
        item
        for item in evidence.change_set_validations
        if item.change_set_id == change_set.change_set_id
        and item.workspace_id == change_set.workspace_id
        and item.snapshot_id == change_set.base_snapshot_id
        and item.passed
        and not item.issues
    )
    mutations = tuple(
        item
        for item in evidence.mutations
        if item.change_set_id == change_set.change_set_id
        and item.task_id == attempt.task.task_id
        and item.request_id == attempt.request.request_id
        and item.attempt_id == attempt.request.attempt_id
    )
    if (
        base_snapshot is None
        or base_snapshot.workspace_id != change_set.workspace_id
        or not workspace_snapshot_identity_is_valid(base_snapshot)
        or len(change_validations) != 1
        or len(mutations) != 1
    ):
        return (), tuple(retained), True
    mutation = mutations[0]
    pre_snapshot = next(
        (
            item
            for item in evidence.snapshots
            if item.snapshot_id == mutation.pre_mutation_snapshot_id
        ),
        None,
    )
    post_snapshot = next(
        (
            item
            for item in evidence.snapshots
            if item.snapshot_id == mutation.post_mutation_snapshot_id
        ),
        None,
    )
    if (
        mutation.status is not WorkspaceMutationStatus.APPLIED
        or mutation.workspace_id != change_set.workspace_id
        or mutation.base_snapshot_id != change_set.base_snapshot_id
        or mutation.issues
        or not workspace_mutation_result_identity_is_valid(mutation)
        or mutation.mutation_id not in attempt.exit_decision.evidence_ids
        or pre_snapshot is None
        or pre_snapshot.workspace_id != change_set.workspace_id
        or not workspace_snapshot_identity_is_valid(pre_snapshot)
        or post_snapshot is None
        or post_snapshot.workspace_id != change_set.workspace_id
        or not workspace_snapshot_identity_is_valid(post_snapshot)
    ):
        return (), tuple(retained), True
    artifacts_by_id = {item.artifact_id: item for item in attempt.artifacts}
    intents = {
        (item.artifact_id, item.target_path) for item in materialization.intents
    }
    links: list[TraceabilityImplementationLink] = []
    for change in change_set.file_changes:
        artifact = artifacts_by_id.get(change.artifact_id)
        matching_file_evidence = tuple(
            item
            for item in mutation.file_evidence
            if item.path == change.path
            and item.operation is change.operation
            and item.expected_preimage_hash == change.expected_preimage_hash
            and item.observed_preimage_hash == change.expected_preimage_hash
            and item.desired_postimage_hash == change.desired_content_hash
            and item.observed_postimage_hash == change.desired_content_hash
            and not item.rollback_attempted
            and not item.rollback_verified
        )
        postimage = post_snapshot.file_state(change.path)
        if (
            artifact is None
            or (change.artifact_id, change.path) not in intents
            or change.artifact_lineage_id != artifact.lineage_id
            or change.desired_content != artifact.content
            or change.desired_content_hash
            != workspace_file_content_hash(artifact.content)
            or len(matching_file_evidence) != 1
            or postimage is None
            or postimage.content_hash != change.desired_content_hash
        ):
            return (), tuple(retained), True
        file_evidence = matching_file_evidence[0]
        if change.operation is WorkspaceChangeOperation.NO_CHANGE:
            if file_evidence.write_performed:
                return (), tuple(retained), True
        elif not file_evidence.write_performed:
            return (), tuple(retained), True
        links.append(
            TraceabilityImplementationLink(
                artifact_id=artifact.artifact_id,
                task_id=attempt.task.task_id,
                target_path=change.path,
                operation=change.operation,
                materialization_validation_id=(
                    materialization.materialization_validation_id
                ),
                change_set_id=change_set.change_set_id,
                mutation_id=mutation.mutation_id,
                expected_preimage_hash=change.expected_preimage_hash,
                observed_postimage_hash=change.desired_content_hash,
            )
        )
    if len(links) != len(mutation.file_evidence):
        return (), tuple(retained), True
    retained.extend(
        (
            TraceabilityEvidenceLink(
                evidence_id=change_set.change_set_id,
                evidence_kind="WORKSPACE_CHANGE_SET",
                task_id=attempt.task.task_id,
            ),
            TraceabilityEvidenceLink(
                evidence_id=mutation.mutation_id,
                evidence_kind="WORKSPACE_MUTATION",
                task_id=attempt.task.task_id,
            ),
        )
    )
    return tuple(links), tuple(retained), False


def _final_authority(
    state: Mapping[str, Any],
    evidence: _EvidenceState,
    export_result: ProjectExportResult | None,
) -> tuple[TraceabilityFinalAuthority, bool]:
    gaps: list[str] = []
    readiness = _optional_model(
        state.get("project_readiness_validation"),
        ProjectReadinessValidation,
        "project_readiness_validation",
        gaps,
    )
    final_snapshot = None
    if readiness is not None and readiness.final_workspace_snapshot_id is not None:
        final_snapshot = next(
            (
                item
                for item in evidence.snapshots
                if item.snapshot_id == readiness.final_workspace_snapshot_id
            ),
            None,
        )
    snapshot_valid = bool(
        final_snapshot is not None
        and workspace_snapshot_identity_is_valid(final_snapshot)
    )
    execution_succeeded = bool(
        evidence.execution is not None
        and evidence.execution.status is TaskGraphExecutionStatus.SUCCEEDED
    )
    complete = bool(
        state.get("workflow_status") == "success"
        and state.get("exit_gate_passed") is True
        and readiness is not None
        and readiness.passed
        and snapshot_valid
        and execution_succeeded
        and not evidence.load_gaps
        and not gaps
    )
    publication_succeeded = _publication_is_verified(
        export_result,
        readiness.final_workspace_snapshot_id if readiness is not None else None,
    )
    return (
        TraceabilityFinalAuthority(
            workflow_status=str(state.get("workflow_status", "unknown")),
            exit_gate_passed=state.get("exit_gate_passed") is True,
            readiness_validation_id=(
                readiness.readiness_validation_id if readiness is not None else None
            ),
            readiness_passed=bool(readiness is not None and readiness.passed),
            final_workspace_snapshot_id=(
                readiness.final_workspace_snapshot_id if readiness is not None else None
            ),
            publication_project_name=(
                export_result.project_name if export_result is not None else None
            ),
            publication_succeeded=publication_succeeded,
        ),
        complete,
    )


def _brownfield_lineage(
    state: Mapping[str, Any],
    *,
    spec: ApprovedRequirementSpec,
    graph: TaskGraph | None,
    evidence: _EvidenceState,
    final_authority: TraceabilityFinalAuthority,
    final_authority_complete: bool,
    export_result: ProjectExportResult | None,
) -> BrownfieldTraceabilityLineage | None:
    baseline_value = state.get("brownfield_baseline")
    context_value = state.get("brownfield_codebase_context")
    if (
        baseline_value is None
        and context_value is None
        and spec.requirement_type != "brownfield"
    ):
        return None
    gaps: list[TraceabilityGap] = []
    try:
        baseline = _required_model(
            baseline_value,
            BrownfieldBaselineProvenance,
            "brownfield baseline",
        )
        context = _required_model(
            context_value,
            BrownfieldCodebaseContext,
            "brownfield codebase context",
        )
    except TraceabilityProjectionError as error:
        return BrownfieldTraceabilityLineage(
            verified=False,
            steps=(),
            gaps=(
                TraceabilityGap(
                    code=TraceabilityGapCode.BROWNFIELD_CORRELATION,
                    detail=str(error),
                ),
            ),
        )
    baseline_snapshot = next(
        (
            item
            for item in evidence.snapshots
            if item.snapshot_id == baseline.governed_baseline_snapshot_id
        ),
        None,
    )
    impact = spec.brownfield_impact
    if (
        context.baseline_id != baseline.baseline_id
        or context.selected_project_name != baseline.selected_project_name
        or context.binding.workspace_id != baseline.seed_result.workspace_id
        or context.binding.snapshot_id != baseline.governed_baseline_snapshot_id
        or tuple((item.path, item.content_hash) for item in context.files)
        != tuple((item.path, item.content_hash) for item in baseline.engineering_files)
        or baseline_snapshot is None
        or not workspace_snapshot_identity_is_valid(baseline_snapshot)
        or baseline_snapshot.workspace_id != context.binding.workspace_id
        or spec.requirement_type != "brownfield"
        or impact is None
        or impact.baseline_id != baseline.baseline_id
        or impact.codebase_context_id != context.context_id
        or graph is None
        or graph.requirement_spec_id != spec.spec_id
    ):
        gaps.append(
            TraceabilityGap(
                code=TraceabilityGapCode.BROWNFIELD_CORRELATION,
                detail=(
                    "Baseline, seeded snapshot, codebase context, impact analysis, "
                    "specification, and TaskGraph do not form one exact authority chain."
                ),
            )
        )
    if not final_authority_complete:
        gaps.append(
            TraceabilityGap(
                code=TraceabilityGapCode.FINAL_RUN_AUTHORITY,
                detail=(
                    "Workflow success, exit-gate, execution, readiness, and final "
                    "workspace-snapshot authority are incomplete."
                ),
            )
        )
    if export_result is not None and not _publication_is_verified(
        export_result, final_authority.final_workspace_snapshot_id
    ):
        gaps.append(
            TraceabilityGap(
                code=TraceabilityGapCode.PUBLICATION,
                detail=(
                    "No verified publication result is bound to the authoritative "
                    "final workspace snapshot."
                ),
            )
        )
    if gaps:
        return BrownfieldTraceabilityLineage(
            verified=False,
            steps=(),
            gaps=_unique_gaps(gaps),
        )
    assert impact is not None
    assert graph is not None
    impact_count = sum(
        len(group)
        for group in (
            impact.impacted_modules,
            impact.impacted_services,
            impact.impacted_apis,
            impact.impacted_state,
            impact.impacted_flows,
            impact.impacted_tests,
            impact.impacted_documentation,
            impact.architectural_implications,
            impact.preserved_behaviors,
        )
    )
    steps = [
        BrownfieldLineageStep(
            stage="Selected baseline publication",
            identity=baseline.selected_project_name,
            detail=f"Originating governed run: {baseline.originating_run_id}",
            basis=TraceabilityRelationshipBasis.EXPLICIT,
        ),
        BrownfieldLineageStep(
            stage="Baseline identity / integrity",
            identity=baseline.baseline_id,
            detail=(
                f"Source {baseline.source_snapshot_id}; governed seed "
                f"{baseline.governed_baseline_snapshot_id}"
            ),
            basis=TraceabilityRelationshipBasis.DERIVED,
        ),
        BrownfieldLineageStep(
            stage="Bounded codebase context",
            identity=context.context_id,
            detail=f"{len(context.files)} authoritative files",
            basis=TraceabilityRelationshipBasis.DERIVED,
        ),
        BrownfieldLineageStep(
            stage="Approved impact analysis",
            identity=f"{impact.baseline_id} / {impact.codebase_context_id}",
            detail=f"{impact_count} run-level impact findings",
            basis=TraceabilityRelationshipBasis.EXPLICIT,
        ),
        BrownfieldLineageStep(
            stage="New requirement authority",
            identity=f"{spec.spec_id} V{spec.version:03d}",
            detail=f"Analysis revision {spec.source_analysis_revision}",
            basis=TraceabilityRelationshipBasis.EXPLICIT,
        ),
        BrownfieldLineageStep(
            stage="Approved TaskGraph",
            identity=f"{graph.graph_id} V{graph.version:03d}",
            detail=f"{len(graph.tasks)} governed tasks",
            basis=TraceabilityRelationshipBasis.EXPLICIT,
        ),
        BrownfieldLineageStep(
            stage="Governed mutations / final snapshot",
            identity=str(final_authority.final_workspace_snapshot_id),
            detail=f"{len(evidence.mutations)} retained mutation records",
            basis=TraceabilityRelationshipBasis.DERIVED,
        ),
    ]
    if export_result is not None:
        steps.append(
            BrownfieldLineageStep(
                stage="New published project",
                identity=str(export_result.project_name),
                detail="Verified durable publication; baseline remains separate.",
                basis=TraceabilityRelationshipBasis.DERIVED,
            )
        )
    return BrownfieldTraceabilityLineage(
        verified=True,
        steps=tuple(steps),
        gaps=(),
    )


def _publication_is_verified(
    result: ProjectExportResult | None,
    final_snapshot_id: str | None,
) -> bool:
    return bool(
        result is not None
        and result.succeeded
        and result.project_name
        and result.destination_directory is not None
        and final_snapshot_id is not None
        and result.validation.authoritative_snapshot_id == final_snapshot_id
        and result.validation.source_matches_authority
        and result.validation.export_matches_authority
        and result.validation.evidence_source_valid
        and result.validation.staged_evidence_matches
        and result.validation.post_export_evidence_matches
    )


def _required_model(
    value: object,
    model: type[_ModelT],
    label: str,
) -> _ModelT:
    if value is None:
        raise TraceabilityProjectionError(f"Missing {label}.")
    try:
        return model.model_validate(value, strict=False)
    except (ValueError, TypeError) as error:
        raise TraceabilityProjectionError(f"Invalid {label}: {error}") from error


def _optional_model(
    value: object,
    model: type[_ModelT],
    label: str,
    gaps: list[str],
) -> _ModelT | None:
    if value is None:
        return None
    try:
        return model.model_validate(value, strict=False)
    except (ValueError, TypeError) as error:
        gaps.append(f"Invalid {label}: {error}")
        return None


def _model_sequence(
    state: Mapping[str, Any],
    key: str,
    model: type[_ModelT],
    gaps: list[str],
) -> tuple[_ModelT, ...]:
    value = state.get(key, ())
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        gaps.append(f"Invalid {key}: expected a sequence.")
        return ()
    parsed: list[_ModelT] = []
    try:
        for item in value:
            parsed.append(model.model_validate(item, strict=False))
    except (ValueError, TypeError) as error:
        gaps.append(f"Invalid {key}: {error}")
        return ()
    return tuple(parsed)


def _unique_evidence_links(
    values: Sequence[TraceabilityEvidenceLink],
) -> tuple[TraceabilityEvidenceLink, ...]:
    by_identity = {
        (item.evidence_kind, item.evidence_id, item.task_id): item for item in values
    }
    return tuple(
        by_identity[key]
        for key in sorted(
            by_identity,
            key=lambda item: (item[2] or "", item[0], item[1]),
        )
    )


def _unique_gaps(values: Sequence[TraceabilityGap]) -> tuple[TraceabilityGap, ...]:
    by_identity = {(item.code.value, item.detail): item for item in values}
    return tuple(by_identity[key] for key in sorted(by_identity))
