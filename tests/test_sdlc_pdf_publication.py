"""Semantic, rendering, and publication coverage for governed SDLC PDFs."""

from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from pathlib import Path
from typing import cast

import pytest
from pypdf import PdfReader

from agentic_sdlc.application import GovernedRunMode, GovernedRunRequest
from agentic_sdlc.llm import FakeTaskPlanningClient
from agentic_sdlc.pdf_renderer import PDFRenderer
from agentic_sdlc.requirement_analysis import RequirementAnalysis
from agentic_sdlc.requirement_spec import build_approved_requirement_spec
from agentic_sdlc.sdlc_document_builder import build_sdlc_documents
from agentic_sdlc.sdlc_document_models import (
    DESIGN_SPECIFICATION_PDF,
    REQUIREMENTS_SPECIFICATION_PDF,
    SDLC_PDF_FILENAMES,
    TEST_PLAN_VALIDATION_REPORT_PDF,
    SDLCDocument,
    SDLCDocumentKind,
)
from agentic_sdlc.sdlc_pdf_publication import (
    SDLCPDFPublicationError,
    write_sdlc_pdf_artifacts,
)
from agentic_sdlc.state import WorkflowState, demo_input
from agentic_sdlc.task_graph import (
    ProposedTask,
    ProposedTaskGraph,
    TaskMaterializationPolicy,
    TaskType,
    normalize_and_validate_task_graph,
)
from agentic_sdlc.traceability import TraceabilityStatus, build_requirement_traceability
from tests.test_application import _service
from tests.test_brownfield_baseline import _publish_project
from tests.test_brownfield_reasoning import _ContextAwareAnalyst
from tests.test_governed_validation_workflow import (
    ScriptedValidationExecutor,
    _run as _run_compile,
)
from tests.test_task_execution_workflow import (
    DeterministicExecutor,
    MaterializingExecutor,
    _run_approved,
    _single_proposal,
    _task,
)
from tests.test_workflow import _proposal as _runnable_proposal


@pytest.fixture(scope="module")
def verified_state() -> WorkflowState:
    return _run_compile(ScriptedValidationExecutor())


@pytest.fixture(scope="module")
def unverified_state() -> WorkflowState:
    proposal = ProposedTaskGraph(
        tasks=[
            _task(
                "test_named_but_not_validated",
                depends_on=[],
                materialization_policy=TaskMaterializationPolicy.REQUIRED,
            )
        ]
    )
    return _run_approved(
        proposal,
        MaterializingExecutor({"TASK-001": "tests/test_candidate.py"}),
    )


@pytest.fixture(scope="module")
def not_implemented_state() -> WorkflowState:
    return _run_approved(_single_proposal(), DeterministicExecutor())


@pytest.fixture(scope="module")
def brownfield_terminal(tmp_path_factory: pytest.TempPathFactory):
    root = tmp_path_factory.mktemp("sdlc-pdf-brownfield")
    _publish_project(root)
    service, _, _ = _service(
        root,
        analyst=_ContextAwareAnalyst(blocked_revisions=(False,)),
        planner=FakeTaskPlanningClient([_runnable_proposal()]),
        run_suffix="sdlc-pdf-brownfield",
    )
    requirement_review = service.start_run(
        GovernedRunRequest(
            command="run",
            workflow_input=demo_input(),
            requested_project_name="pdf-enhanced-project",
            run_mode=GovernedRunMode.BROWNFIELD,
            baseline_project_name="published-project",
        )
    )
    assert requirement_review.human_gate is not None
    graph_review = service.resume_run(
        requirement_review.run_id,
        {"decision": "APPROVE", "feedback": ""},
        gate_token=requirement_review.human_gate.gate_token,
    )
    assert graph_review.human_gate is not None
    return service.resume_run(
        graph_review.run_id,
        {"decision": "APPROVE", "feedback": ""},
        gate_token=graph_review.human_gate.gate_token,
    )


def _text(document: SDLCDocument) -> str:
    return document.searchable_text()


def _pdf_text(path: Path) -> tuple[PdfReader, str]:
    reader = PdfReader(path)
    return reader, "\n".join(page.extract_text() or "" for page in reader.pages)


def test_builds_exact_deterministic_document_set_from_governed_state(
    verified_state: WorkflowState,
) -> None:
    first = build_sdlc_documents(verified_state)
    second = build_sdlc_documents(verified_state)

    assert first == second
    assert tuple(document.filename for document in first) == SDLC_PDF_FILENAMES
    assert tuple(document.kind for document in first) == tuple(SDLCDocumentKind)
    assert all(document.run_id == verified_state["run_id"] for document in first)
    assert all(
        document.requirement_spec_id
        == verified_state["approved_requirement_spec"]["spec_id"]
        for document in first
    )


def test_canonical_requirement_and_acceptance_ids_are_not_renumbered(
    tmp_path: Path,
) -> None:
    analysis = RequirementAnalysis(
        normalized_problem_statement="Preserve caller-owned identifiers.",
        requirement_type="greenfield",
        functional_requirements=["Named behavior."],
        nonfunctional_requirements=[],
        constraints=[],
        ambiguities=[],
        assumptions=[],
        acceptance_criteria=["Named outcome."],
        risks=[],
        needs_clarification=False,
        confidence=1.0,
    )
    original = build_approved_requirement_spec(
        analysis,
        source_analysis_revision=4,
        created_at="2026-08-18T12:00:00+00:00",
    )
    spec = original.model_copy(
        update={
            "functional_requirements": (
                original.functional_requirements[0].model_copy(
                    update={"item_id": "FR-9"}
                ),
            ),
            "acceptance_criteria": (
                original.acceptance_criteria[0].model_copy(
                    update={"item_id": "AC-1.1"}
                ),
            ),
        }
    )
    graph, semantics = normalize_and_validate_task_graph(
        ProposedTaskGraph(
            tasks=[
                ProposedTask(
                    key="preserve_ids",
                    title="Preserve exact IDs",
                    description="Use the approved canonical identifiers.",
                    task_type=TaskType.IMPLEMENTATION,
                    materialization_policy=TaskMaterializationPolicy.FORBIDDEN,
                    depends_on=[],
                    requirement_refs=["FR-9"],
                    acceptance_criteria_refs=["AC-1.1"],
                    risk_refs=[],
                    ambiguity_refs=[],
                    expected_outputs=["Evidence only"],
                )
            ]
        ),
        spec,
        version=1,
        created_at="2026-08-18T12:01:00+00:00",
    )
    state = cast(
        WorkflowState,
        {
            "run_id": "custom-id-run",
            "project_name": "Custom ID Project",
            "approved_requirement_spec": spec.model_dump(mode="json"),
            "approved_task_graph": graph.model_dump(mode="json"),
            "task_graph_semantics": semantics.model_dump(mode="json"),
            "task_graph_decision": "APPROVE",
            "workflow_status": "success",
            "exit_gate_passed": True,
        },
    )

    documents = build_sdlc_documents(state)
    combined = "\n".join(_text(document) for document in documents)

    assert "FR-9" in combined
    assert "AC-1.1" in combined
    assert "FR-001" not in combined
    assert "AC-001" not in combined

    paths = write_sdlc_pdf_artifacts(state, tmp_path)
    for path in paths:
        _, rendered_text = _pdf_text(path)
        assert "FR-9" in rendered_text
        assert "AC-1.1" in rendered_text
        assert "FR-001" not in rendered_text
        assert "AC-001" not in rendered_text


def test_functional_and_design_mappings_match_existing_traceability_only(
    unverified_state: WorkflowState,
) -> None:
    documents = build_sdlc_documents(unverified_state)
    functional = documents[1]
    design = documents[2]
    projection = build_requirement_traceability(unverified_state)

    assert {row.status for row in projection.rows} == {TraceabilityStatus.UNVERIFIED}
    assert all(row.validation_links == () for row in projection.rows)
    assert "UNVERIFIED" in _text(functional)
    assert "UNVERIFIED" in _text(design)


def test_not_implemented_status_remains_not_implemented(
    not_implemented_state: WorkflowState,
) -> None:
    documents = build_sdlc_documents(not_implemented_state)
    combined = "\n".join(_text(document) for document in documents)

    assert all(
        row.status is TraceabilityStatus.NOT_IMPLEMENTED
        for row in build_requirement_traceability(not_implemented_state).rows
    )
    assert "NOT_IMPLEMENTED" in combined
    assert "UNVERIFIED" not in combined


def test_validation_report_preserves_actual_commands_evidence_and_statuses(
    verified_state: WorkflowState,
) -> None:
    report = build_sdlc_documents(verified_state)[3]
    text = _text(report)
    evidence = (
        *verified_state["task_validation_execution_evidence"],
        *verified_state["final_workspace_validation_execution_evidence"],
    )

    for item in evidence:
        assert item.evidence_id in text
        assert item.validation_requirement_id in text
        assert item.policy_id in text
        assert item.outcome.value in text
        assert " ".join(item.argv) in text
    assert "UNVERIFIED" not in text
    assert "NOT_IMPLEMENTED" not in text


def test_missing_optional_submission_and_history_are_handled_without_fabrication(
    verified_state: WorkflowState,
) -> None:
    state = deepcopy(verified_state)
    state.pop("requirement_submission", None)
    state.pop("requirement_analysis_history", None)
    state.pop("requirement_review_history", None)

    requirements = build_sdlc_documents(state)[0]
    text = _text(requirements)

    assert verified_state["raw_requirement"] in text
    assert "No separate analysis-history record is available" in text
    assert "Brownfield baseline and provenance" not in text


def test_rendered_pdf_set_is_nonempty_parseable_searchable_and_paginated(
    tmp_path: Path,
    verified_state: WorkflowState,
) -> None:
    paths = write_sdlc_pdf_artifacts(verified_state, tmp_path)

    assert tuple(path.name for path in paths) == SDLC_PDF_FILENAMES
    for path in paths:
        reader, text = _pdf_text(path)
        assert path.stat().st_size > 1_000
        assert reader.pages
        assert reader.metadata.title
        assert "governed truth" in text
        assert "FR-001" in text
        assert "AC-001" in text
        assert "Page " in text


class _FailingSecondRenderer(PDFRenderer):
    def __init__(self) -> None:
        self.calls = 0

    def render(self, _document: SDLCDocument, output_path: Path) -> None:
        self.calls += 1
        if self.calls == 2:
            raise RuntimeError("second document failed")
        output_path.write_bytes(b"%PDF-1.4\n" + b"x" * 600)


def test_render_failure_leaves_no_partial_pdf_set(
    tmp_path: Path,
    verified_state: WorkflowState,
) -> None:
    with pytest.raises(SDLCPDFPublicationError, match="second document failed"):
        write_sdlc_pdf_artifacts(
            verified_state,
            tmp_path,
            renderer=_FailingSecondRenderer(),
        )

    assert all(not (tmp_path / name).exists() for name in SDLC_PDF_FILENAMES)
    assert not tuple(tmp_path.glob(".*.pdf.tmp"))


def test_brownfield_pdfs_are_manifest_bound_and_published_byte_for_byte(
    brownfield_terminal,
) -> None:
    assert brownfield_terminal.manifest_path is not None
    assert brownfield_terminal.export_result is not None
    assert brownfield_terminal.export_result.succeeded
    assert brownfield_terminal.export_result.destination_directory is not None
    run_artifacts = brownfield_terminal.artifact_bundle.artifact_dir
    project_artifacts = (
        brownfield_terminal.export_result.destination_directory / "sdlc-artifacts"
    )
    manifest = json.loads(
        brownfield_terminal.manifest_path.read_text(encoding="utf-8")
    )
    records = {record["path"]: record for record in manifest["files"]}

    assert set(SDLC_PDF_FILENAMES) <= records.keys()
    for filename in SDLC_PDF_FILENAMES:
        source = run_artifacts / filename
        published = project_artifacts / filename
        assert source.read_bytes() == published.read_bytes()
        assert records[filename]["sha256"] == hashlib.sha256(source.read_bytes()).hexdigest()
        assert records[filename]["size_bytes"] == source.stat().st_size
        reader, text = _pdf_text(source)
        assert reader.pages
        assert brownfield_terminal.run_id in text

    _, requirements_text = _pdf_text(run_artifacts / REQUIREMENTS_SPECIFICATION_PDF)
    _, design_text = _pdf_text(run_artifacts / DESIGN_SPECIFICATION_PDF)
    _, test_text = _pdf_text(run_artifacts / TEST_PLAN_VALIDATION_REPORT_PDF)
    assert "Verified baseline provenance" in requirements_text
    assert "published-project" in requirements_text
    assert "Baseline to evolved-project lineage" in design_text
    assert "published-project" in design_text
    assert "Governed validation execution" in test_text


def test_greenfield_documents_do_not_invent_brownfield_data(
    verified_state: WorkflowState,
) -> None:
    combined = "\n".join(_text(document) for document in build_sdlc_documents(verified_state))

    assert "Verified baseline provenance" not in combined
    assert "Baseline to evolved-project lineage" not in combined
    assert "Brownfield API behavior changes" not in combined
