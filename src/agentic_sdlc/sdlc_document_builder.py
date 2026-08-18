"""Build deterministic SDLC document views from existing governed evidence."""

from __future__ import annotations

import json
import shlex
from collections.abc import Mapping, Sequence
from typing import Any, TypeVar

from pydantic import BaseModel, ValidationError

from agentic_sdlc.brownfield_baseline import (
    BrownfieldBaselineProvenance,
    brownfield_baseline_from_value,
)
from agentic_sdlc.brownfield_context import (
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
    build_requirement_traceability,
)
from agentic_sdlc.validation_execution_contracts import (
    TaskValidationExecutionEvidence,
    TaskValidationProvisioningEvidence,
)


class SDLCDocumentBuildError(ValueError):
    """Raised when successful governed evidence cannot form valid document views."""


_AUTHORITY_STATEMENT = (
    "This document is a deterministic, non-authoritative projection of existing "
    "governed SDLC evidence. It presents the governed truth; it does not create "
    "requirements, design, implementation, validation, traceability, or publication "
    "authority."
)
_COMMON_SOURCES = (
    "Original immutable requirement submission and governed analysis history",
    "Human-approved authoritative requirement specification",
    "Human-approved canonical TaskGraph",
    "Final-authority engineering, workspace, and validation evidence",
    "Conservative requirement-to-code traceability projection",
)
_COMMON_LIMITATIONS = (
    "Missing relationships remain missing; identifiers, names, prose, paths, and "
    "semantic similarity are not used to infer links.",
    "The PDF files are presentation artifacts and are never read by governance or "
    "execution logic.",
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
            "Document authority and identity",
            introduction=(_AUTHORITY_STATEMENT,),
            entries=(
                DocumentEntry(
                    heading="Governed source identity",
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
        limitations=_COMMON_LIMITATIONS,
    )


def _functional_document(evidence: _BuilderEvidence) -> SDLCDocument:
    sections = [
        _identity_section(evidence),
        _section(
            "Functional overview",
            introduction=(evidence.spec.normalized_problem_statement,),
            entries=(
                DocumentEntry(
                    heading="Approved functional authority",
                    paragraphs=(
                        "The sections below reproduce approved requirement text "
                        "and approved TaskGraph descriptions. Implements and "
                        "Addresses fields come only from explicit TaskGraph "
                        "references.",
                    ),
                ),
            ),
        ),
        _section(
            "Approved functional behavior and interactions",
            introduction=(
                "Each record is one canonical approved task with the exact "
                "requirements and acceptance criteria it explicitly references.",
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
    limitations = [*_COMMON_LIMITATIONS]
    if impact is None:
        limitations.append(
            "The governed evidence has no separate typed API, user-flow, or state-"
            "transition model; exact approved FR/AC text and TaskGraph descriptions "
            "are presented without reclassification or invented behavior."
        )
    else:
        limitations.append(
            "Individual brownfield impact findings have no authoritative item-to-"
            "task edges, so no requirement IDs are assigned to those findings."
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
    limitations = [*_COMMON_LIMITATIONS]
    if not any(task.task_type is TaskType.DESIGN for task in evidence.graph.tasks):
        limitations.append(
            "The approved TaskGraph contains no DESIGN task; this document presents "
            "approved task responsibilities and final engineering inventory without "
            "inventing a separate architecture narrative."
        )
    limitations.append(
        "No authoritative project architecture diagram is retained. The existing "
        "workflow diagram describes orchestrator control flow and is not relabeled "
        "as product architecture."
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
                "The plan is reproduced from approved TEST/VALIDATION tasks and "
                "explicit required validation profiles; no test relationship is "
                "inferred from a test name or source text.",
            ),
            entries=tuple(_test_task_entry(evidence, task) for task in test_tasks),
        ),
        _test_artifact_section(evidence),
        _section(
            "Governed validation execution",
            introduction=(
                "Every record below is retained process evidence. PASS means the "
                "record itself reports a successful governed execution; item-level "
                "validation IDs are shown only when the conservative traceability "
                "projection establishes that exact relationship.",
            ),
            entries=tuple(
                _validation_entry(evidence, item) for item in evidence.validations
            ),
        ),
    ]
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
            *_COMMON_LIMITATIONS,
            "The governed evidence has validation-profile and artifact identities, "
            "not canonical identities for individual test functions. Test names in "
            "source or stdout are therefore not promoted into inferred traceability.",
            "Application-required final-workspace validation is run-level evidence "
            "and does not create item-specific validation links.",
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
        authority_statement=_AUTHORITY_STATEMENT,
        authoritative_sources=_COMMON_SOURCES,
        sections=numbered,
        limitations=limitations,
    )


def _identity_section(evidence: _BuilderEvidence) -> DocumentSection:
    return _section(
        "Document authority and identity",
        introduction=(_AUTHORITY_STATEMENT,),
        entries=(
            DocumentEntry(
                heading="Governed source identity",
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
                    "specification remains authoritative.",
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
                heading="Approved brownfield impact authority",
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


def _engineering_inventory_section(evidence: _BuilderEvidence) -> DocumentSection:
    final_artifact_ids = {
        link.artifact_id
        for row in evidence.projection.rows
        for link in row.artifact_links
    }
    paths_by_artifact: dict[str, set[str]] = {}
    for row in evidence.projection.rows:
        for link in row.implementation_links:
            paths_by_artifact.setdefault(link.artifact_id, set()).add(link.target_path)
    entries = tuple(
        DocumentEntry(
            heading=f"{artifact.artifact_id} — {artifact.logical_name}",
            fields=(
                _field("Artifact type", artifact.artifact_type.value),
                _field("Canonical task", artifact.task_id),
                _field("Materialized paths", _joined(sorted(paths_by_artifact.get(artifact.artifact_id, ())))),
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
            "Only canonical artifacts reachable through successful final-attempt "
            "traceability are included. Artifact content is not reinterpreted into "
            "new architecture claims.",
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
                    "Canonical item mapping",
                    "Not established for this individual impact finding.",
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
                    "Final authoritative snapshot",
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
                    _field("Complete inventory", str(context.complete_authoritative_inventory)),
                    _field("Total retained text bytes", str(context.total_text_bytes)),
                ),
                canonical_identifiers=(context.context_id, context.baseline_id),
            )
        )
        tables = (
            DocumentTable(
                title="Authoritative baseline inventory",
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
                        "No separate authoritative mitigation claim is recorded.",
                    ),
                ),
                canonical_identifiers=(risk.item_id, *tasks),
            )
        )
    return _section(
        "Design risks, trade-offs, and mitigations",
        introduction=(
            "Risk-to-task references are explicit. A task reference is not relabeled "
            "as a mitigation or trade-off decision without separate evidence.",
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


def _test_artifact_section(evidence: _BuilderEvidence) -> DocumentSection:
    final_artifact_ids = {
        link.artifact_id
        for row in evidence.projection.rows
        for link in row.artifact_links
    }
    path_by_artifact = {
        link.artifact_id: link.target_path
        for row in evidence.projection.rows
        for link in row.implementation_links
    }
    entries = tuple(
        DocumentEntry(
            heading=f"{artifact.artifact_id} — {artifact.logical_name}",
            fields=(
                _field("Canonical task", artifact.task_id),
                _field("Implementation", path_by_artifact.get(artifact.artifact_id, "No authoritative materialized path is linked.")),
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
    paragraphs = tuple(
        value
        for value in (
            f"Retained stdout:\n{item.retained_stdout}" if item.retained_stdout else None,
            f"Retained stderr:\n{item.retained_stderr}" if item.retained_stderr else None,
        )
        if value is not None
    )
    return DocumentEntry(
        heading=f"{item.evidence_id} — {item.profile.value}",
        paragraphs=paragraphs,
        fields=(
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
            _field("stdout SHA-256", item.stdout_sha256),
            _field("stderr SHA-256", item.stderr_sha256),
            _field("stdout truncated", str(item.stdout_truncated)),
            _field("stderr truncated", str(item.stderr_truncated)),
        ),
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
                heading=f"{item.evidence_id} — {item.profile.value}",
                paragraphs=tuple(
                    value
                    for value in (
                        f"Retained stdout:\n{item.retained_stdout}" if item.retained_stdout else None,
                        f"Retained stderr:\n{item.retained_stderr}" if item.retained_stderr else None,
                    )
                    if value is not None
                ),
                fields=(
                    _field("Validation requirement ID", item.validation_requirement_id),
                    _field("Canonical task", item.task_id),
                    _field("Governed command", shlex.join(item.argv)),
                    _field("Dependencies", _joined(item.normalized_dependencies)),
                    _field("Package index", item.package_index_url),
                    _field("Outcome", item.outcome.value),
                    _field("Exit code", str(item.exit_code) if item.exit_code is not None else "None"),
                    _field("Result", "PASS" if item.passed else "NOT PASSED"),
                    _field("Container cleanup succeeded", str(item.container_cleanup_succeeded)),
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
            heading=f"{item.task_id} attempt {item.attempt_number}",
            paragraphs=(item.reason, item.feedback),
            fields=(
                _field("Failure kind", item.failure_kind.value),
                _field("Retryable", str(item.retryable)),
                _field("Recovery action", item.action.value),
                _field("Maximum attempts", str(item.max_attempts)),
                _field("Request ID", item.request_id or "Not established"),
                _field("Attempt ID", item.attempt_id or "Not established"),
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


def _final_validation_outcome_section(evidence: _BuilderEvidence) -> DocumentSection:
    final = evidence.projection.final_authority
    fields = [
        _field("Workflow status", final.workflow_status),
        _field("Exit gate", "PASS" if final.exit_gate_passed else "NOT PASSED"),
        _field("Readiness validation ID", final.readiness_validation_id or "Not established"),
        _field("Readiness", "PASS" if final.readiness_passed else "NOT PASSED"),
        _field("Final authoritative snapshot", final.final_workspace_snapshot_id or "Not established"),
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
        introduction=(
            "Statuses and relationships are copied from the conservative existing "
            "traceability projection. Missing joins remain missing.",
        ),
        tables=(
            DocumentTable(
                title="Requirement and acceptance-criterion traceability",
                columns=("ID", "Description", "Tasks", "Implementation", "Validation", "Status"),
                rows=tuple(_traceability_table_row(row) for row in projection.rows),
            ),
        ),
    )


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
