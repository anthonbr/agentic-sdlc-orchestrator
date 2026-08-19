"""Build deterministic SDLC document views from existing governed evidence."""

from __future__ import annotations

import ast
import json
import re
import shlex
from collections.abc import Mapping, Sequence
from typing import Any, TypeVar

from pydantic import BaseModel, ValidationError

from agentic_sdlc.brownfield_baseline import (
    BrownfieldBaselineProvenance,
    brownfield_baseline_from_value,
)
from agentic_sdlc.brownfield_context import (
    BrownfieldCodebaseFileKind,
    BrownfieldCodebaseContext,
    brownfield_codebase_context_from_value,
)
from agentic_sdlc.project_readiness import ProjectReadinessValidation
from agentic_sdlc.requirement_spec import ApprovedRequirementSpec, RequirementSpecItem
from agentic_sdlc.sdlc_document_models import (
    DESIGN_SPECIFICATION_PDF,
    FUNCTIONAL_SPECIFICATION_PDF,
    REQUIREMENTS_SPECIFICATION_PDF,
    SDLC_DOCUMENT_SCHEMA_VERSION,
    TEST_PLAN_VALIDATION_REPORT_PDF,
    DocumentEntry,
    DocumentField,
    DocumentSection,
    DocumentTable,
    SDLCDocument,
    SDLCDocumentKind,
)
from agentic_sdlc.task_execution import TaskExecutionRecoveryDecision
from agentic_sdlc.task_execution_contracts import EngineeringArtifact
from agentic_sdlc.task_graph import Task, TaskGraph, TaskType
from agentic_sdlc.traceability import (
    RequirementTraceabilityProjection,
    TraceabilityItemKind,
    TraceabilityRow,
    TraceabilityStatus,
    build_requirement_traceability,
)
from agentic_sdlc.validation_execution_contracts import (
    TaskValidationExecutionEvidence,
    TaskValidationProvisioningEvidence,
)


class SDLCDocumentBuildError(ValueError):
    """Raised when successful governed evidence cannot form valid document views."""


_PROVENANCE_SENTENCE = (
    "This report is generated from the governed SDLC evidence for this run."
)
_TRACEABILITY_INTRODUCTION = (
    "Traceability reflects relationships established during the governed workflow. "
    "Relationships are reported only where they were established by the governed "
    "workflow."
)
_COMPLETE_OUTPUT_NOTE = (
    "Complete stdout and stderr are retained in immutable evidence {evidence_id}."
)
_COMMON_SOURCES = (
    "Original immutable requirement submission and governed analysis history",
    "Human-approved authoritative requirement specification",
    "Human-approved canonical TaskGraph",
    "Final engineering, workspace, and validation evidence",
    "Governed requirement-to-code traceability",
)
_ModelT = TypeVar("_ModelT", bound=BaseModel)


def build_sdlc_documents(state: Mapping[str, Any]) -> tuple[SDLCDocument, ...]:
    """Build all four publication views without I/O or another model call."""

    if state.get("workflow_status") != "success" or state.get(
        "exit_gate_passed"
    ) is not True:
        raise SDLCDocumentBuildError(
            "Governed SDLC documents require a successful exit-gated workflow."
        )
    run_id = _required_text(state.get("run_id"), "run ID")
    project_name = _required_text(state.get("project_name"), "project name")
    spec = _model(
        state.get("approved_requirement_spec"),
        ApprovedRequirementSpec,
        "approved requirement specification",
    )
    graph = _model(
        state.get("approved_task_graph"),
        TaskGraph,
        "approved TaskGraph",
    )
    if (
        graph.requirement_spec_id != spec.spec_id
        or graph.requirement_spec_version != spec.version
        or state.get("task_graph_decision") != "APPROVE"
    ):
        raise SDLCDocumentBuildError(
            "Approved TaskGraph authority does not match the approved specification."
        )
    projection = build_requirement_traceability(state)
    evidence = _BuilderEvidence(
        state=state,
        run_id=run_id,
        project_name=project_name,
        spec=spec,
        graph=graph,
        projection=projection,
        artifacts=_model_sequence(state, "engineering_artifacts", EngineeringArtifact),
        validations=(
            *_model_sequence(
                state,
                "task_validation_execution_evidence",
                TaskValidationExecutionEvidence,
            ),
            *_model_sequence(
                state,
                "final_workspace_validation_execution_evidence",
                TaskValidationExecutionEvidence,
            ),
        ),
        provisioning=(
            *_model_sequence(
                state,
                "task_validation_provisioning_evidence",
                TaskValidationProvisioningEvidence,
            ),
            *_model_sequence(
                state,
                "final_workspace_validation_provisioning_evidence",
                TaskValidationProvisioningEvidence,
            ),
        ),
        recoveries=_model_sequence(
            state,
            "task_execution_recovery_decisions",
            TaskExecutionRecoveryDecision,
        ),
        readiness=_optional_model(
            state.get("project_readiness_validation"),
            ProjectReadinessValidation,
            "project readiness validation",
        ),
        baseline=_brownfield_baseline(state),
        codebase_context=_brownfield_context(state),
    )
    return (
        _requirements_document(evidence),
        _functional_document(evidence),
        _design_document(evidence),
        _test_document(evidence),
    )


class _BuilderEvidence:
    """Validated inputs shared by the four deterministic builders."""

    def __init__(
        self,
        *,
        state: Mapping[str, Any],
        run_id: str,
        project_name: str,
        spec: ApprovedRequirementSpec,
        graph: TaskGraph,
        projection: RequirementTraceabilityProjection,
        artifacts: tuple[EngineeringArtifact, ...],
        validations: tuple[TaskValidationExecutionEvidence, ...],
        provisioning: tuple[TaskValidationProvisioningEvidence, ...],
        recoveries: tuple[TaskExecutionRecoveryDecision, ...],
        readiness: ProjectReadinessValidation | None,
        baseline: BrownfieldBaselineProvenance | None,
        codebase_context: BrownfieldCodebaseContext | None,
    ) -> None:
        self.state = state
        self.run_id = run_id
        self.project_name = project_name
        self.spec = spec
        self.graph = graph
        self.projection = projection
        self.artifacts = artifacts
        self.validations = validations
        self.provisioning = provisioning
        self.recoveries = recoveries
        self.readiness = readiness
        self.baseline = baseline
        self.codebase_context = codebase_context
        self.item_by_id = {item.item_id: item for item in spec.all_items()}
        self.task_by_id = {task.task_id: task for task in graph.tasks}
        self.row_by_id = {row.item_id: row for row in projection.rows}


def _requirements_document(evidence: _BuilderEvidence) -> SDLCDocument:
    spec = evidence.spec
    sections = [
        _section(
            "Document identity",
            entries=(
                DocumentEntry(
                    heading="Run and specification",
                    fields=(
                        _field("Project / product", evidence.project_name),
                        _field("Run ID", evidence.run_id),
                        _field("Specification ID", spec.spec_id),
                        _field("Specification version", str(spec.version)),
                        _field(
                            "Source analysis revision",
                            str(spec.source_analysis_revision),
                        ),
                        _field("Requirement type", spec.requirement_type),
                        _field(
                            "Requirement status",
                            _requirement_status(evidence.state),
                        ),
                    ),
                    canonical_identifiers=(spec.spec_id,),
                ),
            ),
        ),
        _requirement_submission_section(evidence),
        _analysis_revision_section(evidence),
        _spec_item_section(
            evidence,
            "Functional requirements",
            spec.functional_requirements,
        ),
        _spec_item_section(
            evidence,
            "Nonfunctional requirements",
            spec.nonfunctional_requirements,
        ),
        _spec_item_section(
            evidence,
            "Acceptance criteria",
            spec.acceptance_criteria,
        ),
        _spec_item_section(evidence, "Constraints", spec.constraints),
        _plain_list_section("Assumptions", spec.assumptions),
        _spec_item_section(evidence, "Risks", spec.risks),
        _spec_item_section(
            evidence,
            "Retained ambiguities, limitations, and exclusions",
            spec.ambiguities,
            empty_text=(
                "No ambiguity item remains in the approved specification. This "
                "does not imply that unsupported design detail or validation "
                "relationships were inferred."
            ),
        ),
    ]
    if evidence.baseline is not None:
        sections.append(_requirements_brownfield_section(evidence))
    sections.append(_traceability_section(evidence.projection))
    return _document(
        evidence,
        kind=SDLCDocumentKind.REQUIREMENTS_SPECIFICATION,
        filename=REQUIREMENTS_SPECIFICATION_PDF,
        title="Requirements Specification",
        sections=sections,
        limitations=(),
    )


def _functional_document(evidence: _BuilderEvidence) -> SDLCDocument:
    sections = [
        _identity_section(evidence),
        _section(
            "Functional overview",
            introduction=(evidence.spec.normalized_problem_statement,),
            entries=(
                DocumentEntry(
                    heading="Approved functional scope",
                    paragraphs=(
                        "The sections below organize approved requirements and "
                        "TaskGraph responsibilities into the externally observable "
                        "behavior of the system.",
                    ),
                ),
            ),
        ),
        _section(
            "Approved functional behavior and interactions",
            introduction=(
                "Each behavior is associated with its canonical TaskGraph task and "
                "the requirement and acceptance-criterion coverage established for "
                "that task.",
            ),
            entries=tuple(_functional_task_entry(evidence, task) for task in evidence.graph.tasks),
        ),
    ]
    impact = evidence.spec.brownfield_impact
    if impact is not None:
        sections.extend(
            (
                _impact_section(
                    "Brownfield API behavior changes",
                    impact.impacted_apis,
                ),
                _impact_section(
                    "Brownfield user and system flows",
                    impact.impacted_flows,
                ),
                _impact_section(
                    "Brownfield state transitions and preserved behavior",
                    (*impact.impacted_state, *impact.preserved_behaviors),
                ),
            )
        )
    sections.extend(
        (
            _spec_item_section(
                evidence,
                "Relevant assumptions and constraints",
                (*evidence.spec.constraints,),
                introductory_values=evidence.spec.assumptions,
            ),
            _traceability_section(evidence.projection),
        )
    )
    limitations = []
    if impact is None:
        limitations.append(
            "This run does not contain a separate typed API, user-flow, or state-"
            "transition model. Functional behavior is therefore organized from the "
            "approved requirements and TaskGraph descriptions."
        )
    else:
        limitations.append(
            "Individual brownfield impact findings do not have item-to-task "
            "relationships, so those findings are presented without requirement IDs."
        )
    return _document(
        evidence,
        kind=SDLCDocumentKind.FUNCTIONAL_SPECIFICATION,
        filename=FUNCTIONAL_SPECIFICATION_PDF,
        title="Functional Specification",
        sections=sections,
        limitations=tuple(limitations),
    )


def _design_document(evidence: _BuilderEvidence) -> SDLCDocument:
    sections = [
        _identity_section(evidence),
        _section(
            "Architecture and design overview",
            introduction=(evidence.spec.normalized_problem_statement,),
            entries=tuple(_design_task_entry(evidence, task) for task in evidence.graph.tasks),
        ),
        _engineering_inventory_section(evidence),
    ]
    impact = evidence.spec.brownfield_impact
    if impact is not None:
        sections.extend(
            _impact_section(title, values)
            for title, values in (
                ("Brownfield impacted modules and services", (*impact.impacted_modules, *impact.impacted_services)),
                ("Brownfield API and data/state design", (*impact.impacted_apis, *impact.impacted_state)),
                ("Brownfield data and control flows", impact.impacted_flows),
                ("Brownfield architectural implications", impact.architectural_implications),
                ("Brownfield preserved behavior", impact.preserved_behaviors),
            )
        )
        sections.append(_design_brownfield_lineage_section(evidence))
    sections.extend(
        (
            _design_risk_section(evidence),
            _spec_item_section(
                evidence,
                "Security, reliability, and constraints",
                (*evidence.spec.nonfunctional_requirements, *evidence.spec.constraints),
            ),
            _traceability_section(evidence.projection),
        )
    )
    limitations = []
    if not any(task.task_type is TaskType.DESIGN for task in evidence.graph.tasks):
        limitations.append(
            "The approved TaskGraph contains no DESIGN task. The design view is "
            "therefore limited to approved task responsibilities and the final "
            "engineering inventory."
        )
    limitations.append(
        "This run does not contain a separate product architecture diagram. The "
        "workflow diagram documents orchestrator control flow rather than product "
        "architecture."
    )
    return _document(
        evidence,
        kind=SDLCDocumentKind.DESIGN_SPECIFICATION,
        filename=DESIGN_SPECIFICATION_PDF,
        title="Design Specification",
        sections=sections,
        limitations=tuple(limitations),
    )


def _test_document(evidence: _BuilderEvidence) -> SDLCDocument:
    test_tasks = tuple(
        task
        for task in evidence.graph.tasks
        if task.task_type in {TaskType.TEST, TaskType.VALIDATION}
        or task.required_validations
    )
    sections = [
        _identity_section(evidence),
        _section(
            "Test strategy and approved validation plan",
            introduction=(
                "The validation plan follows the approved TEST and VALIDATION tasks "
                "and their required validation profiles. Test inventory names are "
                "informational and do not establish traceability relationships.",
            ),
            entries=tuple(_test_task_entry(evidence, task) for task in test_tasks),
        ),
    ]
    test_inventory = _python_test_inventory_section(evidence)
    if test_inventory is not None:
        sections.append(test_inventory)
    sections.extend(
        (
            _test_artifact_section(evidence),
            _section(
            "Governed validation execution",
            introduction=(
                "Each record summarizes one governed validation attempt. Complete "
                "stdout and stderr remain available through the immutable evidence "
                "identifier shown for that attempt.",
            ),
            entries=tuple(
                _validation_entry(evidence, item) for item in evidence.validations
            ),
            ),
        )
    )
    if evidence.provisioning:
        sections.append(_provisioning_section(evidence))
    sections.extend(
        (
            _retry_section(evidence),
            _final_validation_outcome_section(evidence),
            _traceability_section(evidence.projection),
        )
    )
    return _document(
        evidence,
        kind=SDLCDocumentKind.TEST_PLAN_VALIDATION_REPORT,
        filename=TEST_PLAN_VALIDATION_REPORT_PDF,
        title="Test Plan and Validation Report",
        sections=sections,
        limitations=(
            "Validation evidence identifies profiles and artifacts, but does not "
            "assign canonical IDs to individual test functions. Test inventory names "
            "do not establish requirement or acceptance-criterion relationships.",
            "Final-workspace validation is recorded at the run level and does not "
            "establish item-specific validation relationships.",
        ),
    )


def _document(
    evidence: _BuilderEvidence,
    *,
    kind: SDLCDocumentKind,
    filename: str,
    title: str,
    sections: Sequence[DocumentSection],
    limitations: tuple[str, ...],
) -> SDLCDocument:
    numbered = tuple(
        section.model_copy(update={"number": index})
        for index, section in enumerate(sections, start=1)
    )
    return SDLCDocument(
        schema_version=SDLC_DOCUMENT_SCHEMA_VERSION,
        kind=kind,
        filename=filename,
        title=title,
        project_name=evidence.project_name,
        run_id=evidence.run_id,
        requirement_spec_id=evidence.spec.spec_id,
        requirement_spec_version=evidence.spec.version,
        authority_statement=_PROVENANCE_SENTENCE,
        authoritative_sources=_COMMON_SOURCES,
        sections=numbered,
        limitations=limitations,
    )


def _identity_section(evidence: _BuilderEvidence) -> DocumentSection:
    return _section(
        "Document identity",
        entries=(
            DocumentEntry(
                heading="Run and specification",
                fields=(
                    _field("Project / product", evidence.project_name),
                    _field("Run ID", evidence.run_id),
                    _field("Specification ID", evidence.spec.spec_id),
                    _field("Specification version", str(evidence.spec.version)),
                    _field("TaskGraph ID", evidence.graph.graph_id),
                    _field("TaskGraph version", str(evidence.graph.version)),
                ),
                canonical_identifiers=(evidence.spec.spec_id, evidence.graph.graph_id),
            ),
        ),
    )


def _requirement_submission_section(evidence: _BuilderEvidence) -> DocumentSection:
    submission = evidence.state.get("requirement_submission")
    if isinstance(submission, Mapping):
        original = _text_or_none(submission.get("original_text"))
        normalized = _text_or_none(submission.get("normalized_text"))
        fields = tuple(
            field
            for field in (
                _optional_field("Source kind", submission.get("source_kind")),
                _optional_field("Source filename", submission.get("source_filename")),
                _optional_field("Original SHA-256", submission.get("original_sha256")),
                _optional_field("Normalized SHA-256", submission.get("normalized_sha256")),
            )
            if field is not None
        )
    else:
        original = _text_or_none(evidence.state.get("raw_requirement"))
        normalized = original
        fields = ()
    entries = [
        DocumentEntry(
            heading="Original submitted requirement",
            paragraphs=(original or "Original submitted text is not retained.",),
            fields=fields,
        )
    ]
    if normalized and normalized != original:
        entries.append(
            DocumentEntry(
                heading="Deterministically normalized workflow requirement",
                paragraphs=(normalized,),
            )
        )
    entries.append(
        DocumentEntry(
            heading="Approved normalized problem statement",
            paragraphs=(evidence.spec.normalized_problem_statement,),
        )
    )
    return _section("Requirement submission", entries=tuple(entries))


def _analysis_revision_section(evidence: _BuilderEvidence) -> DocumentSection:
    entries = []
    for ordinal, value in enumerate(
        _mapping_sequence(evidence.state, "requirement_analysis_history"), start=1
    ):
        analysis = value.get("analysis")
        readiness = value.get("planning_readiness")
        paragraphs = ()
        if isinstance(analysis, Mapping):
            problem = _text_or_none(analysis.get("normalized_problem_statement"))
            ambiguities = _string_values(analysis.get("ambiguities"))
            assumptions = _string_values(analysis.get("assumptions"))
            paragraphs = tuple(
                item
                for item in (
                    problem,
                    "Ambiguities: " + "; ".join(ambiguities) if ambiguities else None,
                    "Assumptions: " + "; ".join(assumptions) if assumptions else None,
                )
                if item
            )
        fields = [
            _field("Sequence", str(value.get("sequence", ordinal))),
            _field("Revision", str(value.get("revision_number", "not recorded"))),
            _field("Attempt", str(value.get("attempt_number", "not recorded"))),
        ]
        for label, item in (
            ("Prompt version", value.get("prompt_version")),
            ("Model", value.get("model_name")),
            ("Reviewer feedback", value.get("reviewer_feedback")),
        ):
            field = _optional_field(label, item)
            if field is not None:
                fields.append(field)
        if isinstance(readiness, Mapping):
            field = _optional_field("Planning readiness", readiness.get("status"))
            if field is not None:
                fields.append(field)
        entries.append(
            DocumentEntry(
                heading=f"Analysis revision {value.get('revision_number', ordinal)}",
                paragraphs=paragraphs,
                fields=tuple(fields),
            )
        )
    if not entries:
        entries.append(
            DocumentEntry(
                heading="Approved analysis revision",
                paragraphs=(
                    "No separate analysis-history record is available; the approved "
                    "specification contains the current approved analysis.",
                ),
            )
        )
    review_rows = tuple(
        (
            str(value.get("sequence", index)),
            str(value.get("revision_number", "not recorded")),
            str(value.get("decision", "not recorded")),
            str(value.get("feedback") or "None"),
        )
        for index, value in enumerate(
            _mapping_sequence(evidence.state, "requirement_review_history"), start=1
        )
    )
    tables = (
        DocumentTable(
            title="Human requirement-review history",
            columns=("Sequence", "Revision", "Decision", "Feedback"),
            rows=review_rows,
        ),
    ) if review_rows else ()
    return _section(
        "Clarifications and approved requirement revisions",
        entries=tuple(entries),
        tables=tables,
    )


def _spec_item_section(
    evidence: _BuilderEvidence,
    title: str,
    items: Sequence[RequirementSpecItem],
    *,
    empty_text: str = "No item is present in the approved specification.",
    introductory_values: Sequence[str] = (),
) -> DocumentSection:
    introduction = tuple(introductory_values)
    entries = tuple(_spec_item_entry(evidence, item) for item in items)
    if not entries:
        entries = (DocumentEntry(heading="None recorded", paragraphs=(empty_text,)),)
    return _section(title, introduction=introduction, entries=entries)


def _spec_item_entry(
    evidence: _BuilderEvidence,
    item: RequirementSpecItem,
) -> DocumentEntry:
    row = evidence.row_by_id.get(item.item_id)
    fields = [_field("Lineage ID", item.lineage_id)]
    if row is not None:
        fields.extend(
            (
                _field("Traceability status", row.status.value),
                _field("Related tasks", _task_ids(row)),
            )
        )
    return DocumentEntry(
        heading=item.item_id,
        paragraphs=(item.text,),
        fields=tuple(fields),
        canonical_identifiers=(item.item_id,),
    )


def _plain_list_section(title: str, values: Sequence[str]) -> DocumentSection:
    entries = tuple(
        DocumentEntry(heading=f"Recorded {title.casefold()[:-1]}", paragraphs=(value,))
        for value in values
    )
    if not entries:
        entries = (
            DocumentEntry(
                heading="None recorded",
                paragraphs=(f"No {title.casefold()} are recorded.",),
            ),
        )
    return _section(title, entries=entries)


def _requirements_brownfield_section(evidence: _BuilderEvidence) -> DocumentSection:
    baseline = evidence.baseline
    assert baseline is not None
    entries = [
        DocumentEntry(
            heading="Verified baseline provenance",
            fields=(
                _field("Baseline project", baseline.selected_project_name),
                _field("Baseline ID", baseline.baseline_id),
                _field("Originating run ID", baseline.originating_run_id),
                _field("Publication bundle SHA-256", baseline.publication_bundle_sha256),
                _field("Source snapshot ID", baseline.source_snapshot_id),
                _field("Governed baseline snapshot ID", baseline.governed_baseline_snapshot_id),
            ),
            canonical_identifiers=(
                baseline.baseline_id,
                baseline.originating_run_id,
                baseline.source_snapshot_id,
                baseline.governed_baseline_snapshot_id,
            ),
        )
    ]
    impact = evidence.spec.brownfield_impact
    if impact is not None:
        entries.append(
            DocumentEntry(
                heading="Approved brownfield impact",
                fields=(
                    _field("Baseline ID", impact.baseline_id),
                    _field("Codebase context ID", impact.codebase_context_id),
                ),
                canonical_identifiers=(impact.baseline_id, impact.codebase_context_id),
            )
        )
    return _section("Brownfield baseline and provenance", entries=tuple(entries))


def _functional_task_entry(evidence: _BuilderEvidence, task: Task) -> DocumentEntry:
    refs = (*task.requirement_refs, *task.acceptance_criteria_refs)
    paragraphs = [task.description]
    paragraphs.extend(
        f"{reference} — {evidence.item_by_id[reference].text}"
        for reference in refs
        if reference in evidence.item_by_id
    )
    return DocumentEntry(
        heading=f"{task.task_id} — {task.title}",
        paragraphs=tuple(paragraphs),
        fields=(
            _field("Task type", task.task_type.value),
            _field("Implements requirements", _joined(task.requirement_refs)),
            _field("Addresses acceptance criteria", _joined(task.acceptance_criteria_refs)),
            _field("Depends on", _joined(task.depends_on, empty="ENTRY")),
            _field("Expected outputs", _joined(task.expected_outputs)),
        ),
        canonical_identifiers=(task.task_id, *refs),
    )


def _design_task_entry(evidence: _BuilderEvidence, task: Task) -> DocumentEntry:
    refs = (*task.requirement_refs, *task.acceptance_criteria_refs)
    return DocumentEntry(
        heading=f"{task.task_id} — {task.title}",
        paragraphs=(task.description,),
        fields=(
            _field("Task type", task.task_type.value),
            _field("Dependencies / control flow", _joined(task.depends_on, empty="ENTRY")),
            _field("Expected design / engineering outputs", _joined(task.expected_outputs)),
            _field("Requirement IDs addressed", _joined(task.requirement_refs)),
            _field("Acceptance-criterion IDs addressed", _joined(task.acceptance_criteria_refs)),
            _field("Risk IDs referenced", _joined(task.risk_refs)),
        ),
        canonical_identifiers=(task.task_id, *refs, *task.risk_refs),
    )


def _final_artifact_ids(evidence: _BuilderEvidence) -> set[str]:
    return {
        link.artifact_id
        for row in evidence.projection.rows
        for link in row.artifact_links
    }


def _implementation_paths_by_artifact(
    evidence: _BuilderEvidence,
) -> dict[str, tuple[str, ...]]:
    paths: dict[str, set[str]] = {}
    for row in evidence.projection.rows:
        for link in row.implementation_links:
            paths.setdefault(link.artifact_id, set()).add(link.target_path)
    return {
        artifact_id: tuple(sorted(targets))
        for artifact_id, targets in paths.items()
    }


def _engineering_inventory_section(evidence: _BuilderEvidence) -> DocumentSection:
    final_artifact_ids = _final_artifact_ids(evidence)
    paths_by_artifact = _implementation_paths_by_artifact(evidence)
    entries = tuple(
        DocumentEntry(
            heading=f"{artifact.artifact_id} — {artifact.logical_name}",
            fields=(
                _field("Artifact type", artifact.artifact_type.value),
                _field("Canonical task", artifact.task_id),
                _field(
                    "Materialized paths",
                    _joined(paths_by_artifact.get(artifact.artifact_id, ())),
                ),
                _field("Content SHA-256", artifact.content_hash),
                _field("Requirement IDs", _joined(artifact.requirement_refs)),
                _field("Acceptance-criterion IDs", _joined(artifact.acceptance_criteria_refs)),
            ),
            canonical_identifiers=(
                artifact.artifact_id,
                artifact.task_id,
                *artifact.requirement_refs,
                *artifact.acceptance_criteria_refs,
            ),
        )
        for artifact in evidence.artifacts
        if artifact.artifact_id in final_artifact_ids
    )
    return _section(
        "Final engineering artifact inventory",
        introduction=(
            "This inventory lists engineering artifacts associated with successful "
            "final task attempts and their established implementation paths.",
        ),
        entries=entries or (
            DocumentEntry(
                heading="No final artifact inventory available",
                paragraphs=("No final canonical engineering artifact is traceable.",),
            ),
        ),
    )


def _impact_section(title: str, values: Sequence[Any]) -> DocumentSection:
    entries = tuple(
        DocumentEntry(
            heading=value.target,
            paragraphs=(value.reason,),
            fields=(
                _field(
                    "Requirement mapping",
                    "No item-level relationship was established for this impact finding.",
                ),
            ),
        )
        for value in values
    )
    return _section(
        title,
        entries=entries or (
            DocumentEntry(
                heading="None recorded",
                paragraphs=("No supported finding is recorded in this category.",),
            ),
        ),
    )


def _design_brownfield_lineage_section(evidence: _BuilderEvidence) -> DocumentSection:
    baseline = evidence.baseline
    context = evidence.codebase_context
    assert baseline is not None
    entries = [
        DocumentEntry(
            heading="Baseline to evolved-project lineage",
            fields=(
                _field("Baseline project", baseline.selected_project_name),
                _field("Baseline ID", baseline.baseline_id),
                _field("Baseline source snapshot", baseline.source_snapshot_id),
                _field("Seeded governed snapshot", baseline.governed_baseline_snapshot_id),
                _field("Evolved project", evidence.project_name),
                _field("New specification", evidence.spec.spec_id),
                _field("Resulting TaskGraph", evidence.graph.graph_id),
                _field(
                    "Final workspace snapshot",
                    evidence.projection.final_authority.final_workspace_snapshot_id
                    or "Not established",
                ),
            ),
            canonical_identifiers=(
                baseline.baseline_id,
                baseline.source_snapshot_id,
                baseline.governed_baseline_snapshot_id,
                evidence.spec.spec_id,
                evidence.graph.graph_id,
            ),
        )
    ]
    tables = ()
    if context is not None:
        entries.append(
            DocumentEntry(
                heading="Bounded baseline codebase context",
                fields=(
                    _field("Context ID", context.context_id),
                    _field("Baseline ID", context.baseline_id),
                    _field(
                        "Complete baseline inventory",
                        str(context.complete_authoritative_inventory),
                    ),
                    _field("Total retained text bytes", str(context.total_text_bytes)),
                ),
                canonical_identifiers=(context.context_id, context.baseline_id),
            )
        )
        tables = (
            DocumentTable(
                title="Baseline file inventory",
                columns=("Path", "Kind", "Content SHA-256"),
                rows=tuple(
                    (item.path, item.kind.value, item.content_hash)
                    for item in context.files
                ),
            ),
        )
    return _section(
        "Brownfield provenance and baseline evolution",
        entries=tuple(entries),
        tables=tables,
    )


def _design_risk_section(evidence: _BuilderEvidence) -> DocumentSection:
    entries = []
    for risk in evidence.spec.risks:
        tasks = tuple(
            task.task_id for task in evidence.graph.tasks if risk.item_id in task.risk_refs
        )
        entries.append(
            DocumentEntry(
                heading=risk.item_id,
                paragraphs=(risk.text,),
                fields=(
                    _field("Referenced by approved tasks", _joined(tasks)),
                    _field(
                        "Mitigation status",
                        "No separate mitigation is recorded for this risk.",
                    ),
                ),
                canonical_identifiers=(risk.item_id, *tasks),
            )
        )
    return _section(
        "Design risks, trade-offs, and mitigations",
        introduction=(
            "The task references below are the relationships recorded for each risk. "
            "A referenced task is not presented as a mitigation unless a separate "
            "mitigation is recorded.",
        ),
        entries=tuple(entries) or (
            DocumentEntry(
                heading="No approved risk item",
                paragraphs=("The approved specification records no risk item.",),
            ),
        ),
    )


def _test_task_entry(evidence: _BuilderEvidence, task: Task) -> DocumentEntry:
    refs = (*task.requirement_refs, *task.acceptance_criteria_refs)
    return DocumentEntry(
        heading=f"{task.task_id} — {task.title}",
        paragraphs=(task.description,),
        fields=(
            _field("Task type", task.task_type.value),
            _field("Requirements covered by approved plan", _joined(task.requirement_refs)),
            _field("Acceptance criteria covered by approved plan", _joined(task.acceptance_criteria_refs)),
            _field(
                "Required validation profiles",
                _joined(
                    tuple(
                        f"{item.requirement_id}: {item.profile.value}"
                        for item in task.required_validations
                    )
                ),
            ),
            _field("Expected outputs", _joined(task.expected_outputs)),
        ),
        canonical_identifiers=(
            task.task_id,
            *refs,
            *(item.requirement_id for item in task.required_validations),
        ),
    )


def _python_test_inventory_section(
    evidence: _BuilderEvidence,
) -> DocumentSection | None:
    entries = []
    for path, content in _final_python_sources(evidence):
        fields = _python_test_inventory_fields(content)
        if not fields:
            continue
        entries.append(
            DocumentEntry(
                heading=path,
                fields=fields,
            )
        )
    if not entries:
        return None
    return _section(
        "Python test inventory",
        introduction=(
            "Test names are extracted statically from final governed Python files. "
            "They provide an implementation inventory and do not establish "
            "requirement or acceptance-criterion relationships.",
        ),
        entries=tuple(entries),
    )


def _final_python_sources(
    evidence: _BuilderEvidence,
) -> tuple[tuple[str, str], ...]:
    sources: dict[str, str] = {}
    if evidence.codebase_context is not None:
        for item in evidence.codebase_context.files:
            if (
                item.kind is BrownfieldCodebaseFileKind.TEXT
                and item.content is not None
                and item.path.casefold().endswith(".py")
            ):
                sources[item.path] = item.content
    paths_by_artifact = _implementation_paths_by_artifact(evidence)
    final_artifact_ids = _final_artifact_ids(evidence)
    for artifact in evidence.artifacts:
        if artifact.artifact_id not in final_artifact_ids:
            continue
        for path in paths_by_artifact.get(artifact.artifact_id, ()):
            if path.casefold().endswith(".py"):
                sources[path] = artifact.content
    return tuple(sorted(sources.items()))


def _python_test_inventory_fields(content: str) -> tuple[DocumentField, ...]:
    try:
        module = ast.parse(content)
    except (SyntaxError, ValueError, TypeError):
        return ()
    function_types = (ast.FunctionDef, ast.AsyncFunctionDef)
    module_tests = tuple(
        node.name
        for node in module.body
        if isinstance(node, function_types) and node.name.startswith("test_")
    )
    fields = []
    if module_tests:
        fields.append(_field("Module-level tests", "\n".join(module_tests)))
    for node in module.body:
        if not isinstance(node, ast.ClassDef):
            continue
        methods = tuple(
            member.name
            for member in node.body
            if isinstance(member, function_types)
            and member.name.startswith("test_")
        )
        if methods:
            fields.append(_field(node.name, "\n".join(methods)))
    return tuple(fields)


def _test_artifact_section(evidence: _BuilderEvidence) -> DocumentSection:
    final_artifact_ids = _final_artifact_ids(evidence)
    paths_by_artifact = _implementation_paths_by_artifact(evidence)
    entries = tuple(
        DocumentEntry(
            heading=f"{artifact.artifact_id} — {artifact.logical_name}",
            fields=(
                _field("Canonical task", artifact.task_id),
                _field(
                    "Implementation",
                    _joined(
                        paths_by_artifact.get(artifact.artifact_id, ()),
                        empty="No materialized path is linked.",
                    ),
                ),
                _field("Requirement IDs", _joined(artifact.requirement_refs)),
                _field("Acceptance-criterion IDs", _joined(artifact.acceptance_criteria_refs)),
                _field("Content SHA-256", artifact.content_hash),
            ),
            canonical_identifiers=(
                artifact.artifact_id,
                artifact.task_id,
                *artifact.requirement_refs,
                *artifact.acceptance_criteria_refs,
            ),
        )
        for artifact in evidence.artifacts
        if artifact.artifact_id in final_artifact_ids
        and artifact.artifact_type.value in {"TEST", "VALIDATION"}
    )
    return _section(
        "Test and validation implementation artifacts",
        entries=entries or (
            DocumentEntry(
                heading="No final test artifact is traceable",
                paragraphs=(
                    "No canonical final-attempt TEST or VALIDATION artifact with a "
                    "materialization relationship is retained.",
                ),
            ),
        ),
    )


def _validation_entry(
    evidence: _BuilderEvidence,
    item: TaskValidationExecutionEvidence,
) -> DocumentEntry:
    linked_rows = tuple(
        row
        for row in evidence.projection.rows
        if any(link.evidence_id == item.evidence_id for link in row.validation_links)
    )
    validated_ids = tuple(row.item_id for row in linked_rows)
    task = evidence.task_by_id.get(item.task_id)
    plan_refs = (
        (*task.requirement_refs, *task.acceptance_criteria_refs)
        if task is not None
        else ()
    )
    statuses = tuple(dict.fromkeys(row.status.value for row in linked_rows))
    recovery = _recovery_for_attempt(evidence, item.task_id, item.attempt_number)
    fields = [
        _field("Evidence", item.evidence_id),
        _field("Validation profile", item.profile.value),
        _field("Validation requirement ID", item.validation_requirement_id),
        _field("Canonical task", item.task_id),
        _field("Attempt", str(item.attempt_number)),
        _field("Request ID", item.request_id),
        _field("Attempt ID", item.attempt_id),
        _field("Approved task coverage", _joined(plan_refs)),
        _field(
            "Validates canonical IDs",
            _joined(
                validated_ids,
                empty="No item-specific relationship is established.",
            ),
        ),
        _field("Governed command", shlex.join(item.argv)),
        _field("Working directory", item.working_directory),
        _field("Policy", f"{item.policy_id} / {item.policy_version}"),
        _field("Outcome", item.outcome.value),
        _field("Exit code", str(item.exit_code) if item.exit_code is not None else "None"),
        _field("Result", "PASS" if item.passed else "NOT PASSED"),
        _field(
            "Traceability status",
            _joined(statuses, empty="Run-level / no item-specific status"),
        ),
        _field("Started", item.started_at),
        _field("Ended", item.ended_at),
        _field("Duration seconds", f"{item.duration_seconds:.6f}"),
        *_validation_diagnostic_fields(item),
    ]
    if recovery is not None:
        fields.extend(
            (
                _field("Retryable", str(recovery.retryable)),
                _field("Recovery action", recovery.action.value),
                _field(
                    "Subsequent outcome",
                    _subsequent_validation_outcome(
                        evidence,
                        item.task_id,
                        item.attempt_number,
                    ),
                ),
            )
        )
    fields.append(
        _field(
            "Complete output",
            _COMPLETE_OUTPUT_NOTE.format(evidence_id=item.evidence_id),
        )
    )
    return DocumentEntry(
        heading=f"Attempt {item.attempt_number} — {_attempt_result(item)}",
        fields=tuple(fields),
        canonical_identifiers=(
            item.evidence_id,
            item.validation_requirement_id,
            item.task_id,
            item.request_id,
            item.attempt_id,
            *validated_ids,
        ),
    )


def _provisioning_section(evidence: _BuilderEvidence) -> DocumentSection:
    return _section(
        "Governed dependency provisioning",
        entries=tuple(
            DocumentEntry(
                heading=f"Attempt {item.attempt_number} — {_attempt_result(item)}",
                fields=(
                    _field("Evidence", item.evidence_id),
                    _field("Validation profile", item.profile.value),
                    _field("Validation requirement ID", item.validation_requirement_id),
                    _field("Canonical task", item.task_id),
                    _field("Attempt", str(item.attempt_number)),
                    _field("Governed command", shlex.join(item.argv)),
                    _field("Dependencies", _joined(item.normalized_dependencies)),
                    _field("Package source", item.package_index_url),
                    _field("Policy", f"{item.policy_id} / {item.policy_version}"),
                    _field("Outcome", item.outcome.value),
                    _field("Exit code", str(item.exit_code) if item.exit_code is not None else "None"),
                    _field("Result", "PASS" if item.passed else "NOT PASSED"),
                    _field("Duration seconds", f"{item.duration_seconds:.6f}"),
                    _field("Container cleanup succeeded", str(item.container_cleanup_succeeded)),
                    _field(
                        "Complete output",
                        _COMPLETE_OUTPUT_NOTE.format(evidence_id=item.evidence_id),
                    ),
                ),
                canonical_identifiers=(
                    item.evidence_id,
                    item.validation_requirement_id,
                    item.task_id,
                ),
            )
            for item in evidence.provisioning
        ),
    )


def _retry_section(evidence: _BuilderEvidence) -> DocumentSection:
    entries = tuple(
        DocumentEntry(
            heading=f"{item.task_id} — attempt {item.attempt_number} recovery",
            fields=(
                _field("Failure kind", item.failure_kind.value),
                _field("Retryable", str(item.retryable)),
                _field("Recovery action", item.action.value),
                _field("Decision reason", item.reason),
                _field("Maximum attempts", str(item.max_attempts)),
                _field("Request ID", item.request_id or "Not established"),
                _field("Attempt ID", item.attempt_id or "Not established"),
                _field(
                    "Related evidence",
                    _joined(
                        _attempt_evidence_ids(
                            evidence,
                            item.task_id,
                            item.attempt_number,
                        )
                    ),
                ),
                _field(
                    "Subsequent outcome",
                    _subsequent_validation_outcome(
                        evidence,
                        item.task_id,
                        item.attempt_number,
                    ),
                ),
            ),
            canonical_identifiers=tuple(
                value
                for value in (item.task_id, item.request_id, item.attempt_id)
                if value
            ),
        )
        for item in evidence.recoveries
    )
    return _section(
        "Retry and recovery evidence",
        entries=entries or (
            DocumentEntry(
                heading="No retry recorded",
                paragraphs=("No task execution recovery decision is retained.",),
            ),
        ),
    )


def _attempt_result(
    item: TaskValidationExecutionEvidence | TaskValidationProvisioningEvidence,
) -> str:
    return item.outcome.value


def _recovery_for_attempt(
    evidence: _BuilderEvidence,
    task_id: str,
    attempt_number: int,
) -> TaskExecutionRecoveryDecision | None:
    return next(
        (
            item
            for item in evidence.recoveries
            if item.task_id == task_id and item.attempt_number == attempt_number
        ),
        None,
    )


def _attempt_evidence_ids(
    evidence: _BuilderEvidence,
    task_id: str,
    attempt_number: int,
) -> tuple[str, ...]:
    return tuple(
        item.evidence_id
        for item in (*evidence.provisioning, *evidence.validations)
        if item.task_id == task_id and item.attempt_number == attempt_number
    )


def _subsequent_validation_outcome(
    evidence: _BuilderEvidence,
    task_id: str,
    attempt_number: int,
) -> str:
    later = tuple(
        item
        for item in evidence.validations
        if item.task_id == task_id and item.attempt_number > attempt_number
    )
    if later:
        item = min(later, key=lambda candidate: candidate.attempt_number)
        return (
            f"Attempt {item.attempt_number} — {_attempt_result(item)} "
            f"({item.evidence_id})"
        )
    return "No subsequent validation execution is recorded."


def _validation_diagnostic_fields(
    item: TaskValidationExecutionEvidence,
) -> tuple[DocumentField, ...]:
    output = "\n".join(
        value for value in (item.retained_stdout, item.retained_stderr) if value
    )
    summary = _pytest_summary(output)
    failed_tests, failure_details = _pytest_failures(output)
    fields: list[DocumentField] = []
    if failed_tests:
        fields.append(_field("Failed tests", "\n".join(failed_tests)))
    if failure_details:
        fields.append(_field("Failure summary", "\n".join(failure_details)))
    if summary:
        fields.append(_field("Execution summary", summary))
    elif not fields:
        fields.append(
            _field(
                "Execution summary",
                (
                    "Validation completed successfully."
                    if item.passed
                    else f"Validation finished with outcome {item.outcome.value}."
                ),
            )
        )
    return tuple(fields)


def _pytest_failures(output: str) -> tuple[tuple[str, ...], tuple[str, ...]]:
    failed_tests: list[str] = []
    details: list[str] = []
    for line in output.splitlines():
        stripped = line.strip()
        if not stripped.startswith("FAILED "):
            continue
        diagnostic = stripped.removeprefix("FAILED ")
        node_id, separator, detail = diagnostic.partition(" - ")
        if node_id and node_id not in failed_tests:
            failed_tests.append(node_id)
        if separator and detail:
            compact = _compact_diagnostic(detail)
            if compact and compact not in details:
                details.append(compact)
    return tuple(failed_tests[:20]), tuple(details[:5])


def _pytest_summary(output: str) -> str | None:
    status_pattern = re.compile(
        r"\b\d+\s+(?:passed|failed|error|errors|skipped|xfailed|xpassed)\b",
        flags=re.IGNORECASE,
    )
    for line in reversed(output.splitlines()):
        candidate = line.strip().strip("=").strip()
        if status_pattern.search(candidate):
            return _compact_diagnostic(candidate)
    return None


def _compact_diagnostic(value: str, *, limit: int = 240) -> str:
    compact = " ".join(value.split())
    if len(compact) <= limit:
        return compact
    return compact[: limit - 1].rstrip() + "…"


def _final_validation_outcome_section(evidence: _BuilderEvidence) -> DocumentSection:
    final = evidence.projection.final_authority
    fields = [
        _field("Workflow status", final.workflow_status),
        _field("Exit gate", "PASS" if final.exit_gate_passed else "NOT PASSED"),
        _field("Readiness validation ID", final.readiness_validation_id or "Not established"),
        _field("Readiness", "PASS" if final.readiness_passed else "NOT PASSED"),
        _field("Final workspace snapshot", final.final_workspace_snapshot_id or "Not established"),
    ]
    if evidence.readiness is not None:
        fields.extend(
            (
                _field("Required validation count", str(evidence.readiness.runtime_validation_required_count)),
                _field("Verified validation count", str(evidence.readiness.runtime_validation_verified_count)),
                _field("Final-workspace validation required", str(evidence.readiness.final_workspace_validation_required)),
                _field("Final-workspace validation verified", str(evidence.readiness.final_workspace_validation_verified)),
            )
        )
    return _section(
        "Final validation outcome",
        entries=(
            DocumentEntry(
                heading="Governed completion evidence",
                fields=tuple(fields),
                canonical_identifiers=tuple(
                    value
                    for value in (
                        final.readiness_validation_id,
                        final.final_workspace_snapshot_id,
                    )
                    if value
                ),
            ),
        ),
    )


def _traceability_section(
    projection: RequirementTraceabilityProjection,
) -> DocumentSection:
    return _section(
        "Traceability summary",
        introduction=(_traceability_introduction(projection),),
        tables=(
            DocumentTable(
                title="Requirement and acceptance-criterion traceability",
                columns=("ID", "Description", "Tasks", "Implementation", "Validation", "Status"),
                rows=tuple(_traceability_table_row(row) for row in projection.rows),
            ),
        ),
    )


def _traceability_introduction(
    projection: RequirementTraceabilityProjection,
) -> str:
    statuses = {row.status for row in projection.rows}
    sentences = [_TRACEABILITY_INTRODUCTION]
    if TraceabilityStatus.UNVERIFIED in statuses:
        sentences.append(
            "Items without qualifying validation evidence remain UNVERIFIED."
        )
    if TraceabilityStatus.NOT_IMPLEMENTED in statuses:
        sentences.append(
            "Items without established implementation remain NOT_IMPLEMENTED."
        )
    return " ".join(sentences)


def _traceability_table_row(row: TraceabilityRow) -> tuple[str, ...]:
    return (
        row.item_id,
        row.text,
        _joined(tuple(link.task_id for link in row.task_links)),
        _joined(tuple(link.target_path for link in row.implementation_links)),
        _joined(
            tuple(
                f"{link.validation_requirement_id}: {link.profile.value} "
                f"{link.outcome} ({link.evidence_id})"
                for link in row.validation_links
            )
        ),
        row.status.value,
    )


def _section(
    title: str,
    *,
    introduction: tuple[str, ...] = (),
    entries: tuple[DocumentEntry, ...] = (),
    tables: tuple[DocumentTable, ...] = (),
) -> DocumentSection:
    return DocumentSection(
        number=1,
        title=title,
        introduction=introduction,
        entries=entries,
        tables=tables,
    )


def _field(label: str, value: str) -> DocumentField:
    return DocumentField(label=label, value=value or "None")


def _optional_field(label: str, value: object) -> DocumentField | None:
    text = _text_or_none(value)
    return _field(label, text) if text is not None else None


def _joined(values: Sequence[str], *, empty: str = "None") -> str:
    return ", ".join(values) if values else empty


def _task_ids(row: TraceabilityRow) -> str:
    return _joined(tuple(link.task_id for link in row.task_links))


def _requirement_status(state: Mapping[str, Any]) -> str:
    readiness = state.get("requirement_planning_readiness")
    readiness_status = (
        readiness.get("status") if isinstance(readiness, Mapping) else None
    )
    review = state.get("requirement_review_decision")
    values = [str(value) for value in (review, readiness_status) if value]
    return " / ".join(values) or "Not recorded"


def _mapping_sequence(
    state: Mapping[str, Any], key: str
) -> tuple[Mapping[str, Any], ...]:
    value = state.get(key, ())
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        return ()
    return tuple(item for item in value if isinstance(item, Mapping))


def _string_values(value: object) -> tuple[str, ...]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        return ()
    return tuple(item for item in value if isinstance(item, str) and item)


def _text_or_none(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def _required_text(value: object, label: str) -> str:
    text = _text_or_none(value)
    if text is None or not text.strip():
        raise SDLCDocumentBuildError(f"Successful governed evidence lacks {label}.")
    return text


def _model(value: object, model: type[_ModelT], label: str) -> _ModelT:
    if isinstance(value, model):
        return value
    try:
        return model.model_validate_json(json.dumps(value))
    except (TypeError, ValueError, ValidationError) as error:
        raise SDLCDocumentBuildError(f"Invalid {label}: {error}") from error


def _optional_model(
    value: object, model: type[_ModelT], label: str
) -> _ModelT | None:
    return None if value is None else _model(value, model, label)


def _model_sequence(
    state: Mapping[str, Any], key: str, model: type[_ModelT]
) -> tuple[_ModelT, ...]:
    value = state.get(key, ())
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        raise SDLCDocumentBuildError(f"Invalid {key}: expected a sequence.")
    return tuple(_model(item, model, key) for item in value)


def _brownfield_baseline(
    state: Mapping[str, Any],
) -> BrownfieldBaselineProvenance | None:
    value = state.get("brownfield_baseline")
    if value is None:
        return None
    try:
        return brownfield_baseline_from_value(value)
    except (TypeError, ValueError, ValidationError) as error:
        raise SDLCDocumentBuildError(f"Invalid brownfield baseline: {error}") from error


def _brownfield_context(
    state: Mapping[str, Any],
) -> BrownfieldCodebaseContext | None:
    value = state.get("brownfield_codebase_context")
    if value is None:
        return None
    try:
        return brownfield_codebase_context_from_value(value)
    except (TypeError, ValueError, ValidationError) as error:
        raise SDLCDocumentBuildError(f"Invalid brownfield context: {error}") from error
