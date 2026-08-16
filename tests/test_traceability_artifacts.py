"""Focused tests for durable derived traceability reports."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

import agentic_sdlc.traceability_artifacts as traceability_artifacts
from agentic_sdlc.application import GovernedRunMode, GovernedRunRequest
from agentic_sdlc.artifacts import ARTIFACT_FILENAMES, write_artifacts
from agentic_sdlc.llm import FakeTaskPlanningClient
from agentic_sdlc.requirement_analysis import RequirementAnalysis
from agentic_sdlc.requirement_spec import build_approved_requirement_spec
from agentic_sdlc.state import WorkflowState, demo_input
from agentic_sdlc.task_graph import ProposedTaskGraph, TaskMaterializationPolicy
from agentic_sdlc.traceability import (
    TraceabilityRelationshipBasis,
    TraceabilityStatus,
    build_requirement_traceability,
)
from agentic_sdlc.traceability_artifacts import (
    REQUIREMENT_TRACEABILITY_JSON_FILENAME,
    REQUIREMENT_TRACEABILITY_MARKDOWN_FILENAME,
    build_requirement_traceability_artifact,
    render_requirement_traceability_json,
    render_requirement_traceability_markdown,
    write_requirement_traceability_artifacts,
)
from tests.test_application import _service
from tests.test_brownfield_baseline import _publish_project
from tests.test_brownfield_reasoning import _ContextAwareAnalyst
from tests.test_governed_validation_workflow import (
    ScriptedValidationExecutor,
    _run as _run_compile,
)
from tests.test_task_execution_workflow import (
    MaterializingExecutor,
    _run_approved,
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
                "documentation",
                depends_on=[],
                materialization_policy=TaskMaterializationPolicy.REQUIRED,
            )
        ]
    )
    return _run_approved(
        proposal,
        MaterializingExecutor({"TASK-001": "README.md"}),
    )


@pytest.fixture
def brownfield_terminal(tmp_path: Path):
    _publish_project(tmp_path)
    service, _, _ = _service(
        tmp_path,
        analyst=_ContextAwareAnalyst(blocked_revisions=(False,)),
        planner=FakeTaskPlanningClient([_runnable_proposal()]),
        run_suffix="traceability-artifacts-brownfield",
    )
    requirement_review = service.start_run(
        GovernedRunRequest(
            command="run",
            workflow_input=demo_input(),
            requested_project_name="enhanced-project",
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


def test_json_contract_is_deterministic_derived_and_auditable(
    verified_state: WorkflowState,
) -> None:
    projection = build_requirement_traceability(verified_state)
    artifact = build_requirement_traceability_artifact(projection)

    first = render_requirement_traceability_json(artifact)
    second = render_requirement_traceability_json(
        build_requirement_traceability_artifact(
            build_requirement_traceability(verified_state)
        )
    )
    document = json.loads(first)

    assert first == second
    assert document["schema_version"] == "requirement-traceability-v1"
    assert document["artifact_kind"] == "requirement_traceability"
    assert document["authority"] == "DERIVED"
    assert document["authoritative"] is False
    assert document["run_completion_evidence"]["publication_claim"] == (
        "NOT_INCLUDED_PRE_PUBLICATION"
    )
    assert document["status_counts"] == {
        "not_implemented": 0,
        "unverified": 0,
        "verified": 2,
    }
    assert [row["item_id"] for row in document["rows"]] == ["FR-001", "AC-001"]
    assert all(row["status"] == "VERIFIED" for row in document["rows"])
    assert all(row["gaps"] == [] for row in document["rows"])
    assert document["rows"][0]["task_links"][0]["basis"] == (
        TraceabilityRelationshipBasis.EXPLICIT.value
    )
    assert document["rows"][0]["implementation_links"][0]["basis"] == (
        TraceabilityRelationshipBasis.DERIVED.value
    )
    assert "generated_at" not in document


def test_json_contains_every_canonical_namespace_once_in_projection_order() -> None:
    analysis = RequirementAnalysis(
        normalized_problem_statement="Represent each canonical namespace.",
        requirement_type="greenfield",
        functional_requirements=["Functional behavior."],
        nonfunctional_requirements=["Reliable behavior."],
        constraints=["Use the governed workspace."],
        ambiguities=[],
        assumptions=[],
        acceptance_criteria=["Observable outcome."],
        risks=[],
        needs_clarification=False,
        confidence=1.0,
    )
    spec = build_approved_requirement_spec(
        analysis,
        source_analysis_revision=2,
        created_at="2026-08-16T12:00:00+00:00",
    )
    projection = build_requirement_traceability(
        {
            "run_id": "traceability-canonical-report",
            "approved_requirement_spec": spec.model_dump(mode="json"),
        }
    )
    document = json.loads(
        render_requirement_traceability_json(
            build_requirement_traceability_artifact(projection)
        )
    )

    assert [row["item_id"] for row in document["rows"]] == [
        "FR-001",
        "NFR-001",
        "CON-001",
        "AC-001",
    ]
    assert len({row["item_id"] for row in document["rows"]}) == 4
    assert all(row["status"] == "NOT_IMPLEMENTED" for row in document["rows"])
    assert all(row["gaps"] for row in document["rows"])


def test_markdown_is_plain_language_but_preserves_technical_evidence(
    verified_state: WorkflowState,
) -> None:
    report = render_requirement_traceability_markdown(
        build_requirement_traceability_artifact(
            build_requirement_traceability(verified_state)
        )
    )

    assert "# Requirement-to-Code Traceability" in report
    assert "## Traceability Status" in report
    assert (
        "Implemented and explicitly linked to successful governed validation"
        in report
    )
    assert "This does not mean implementation or tests failed" in report
    assert "### FR-001" in report
    assert "**Files changed:** `src/candidate.py` — `CREATE`" in report
    assert "**Validation performed:** `PYTHON_COMPILE` — `PASS`" in report
    assert "PYTHON_PYTEST" not in report
    assert "#### Technical evidence" in report
    assert "semantic-TASK-001" in report
    assert verified_state["task_execution_requests"][0].request_id in report
    assert verified_state["workspace_mutation_results"][0].mutation_id in report


def test_markdown_keeps_missing_validation_unverified_and_visible(
    unverified_state: WorkflowState,
) -> None:
    artifact = build_requirement_traceability_artifact(
        build_requirement_traceability(unverified_state)
    )
    document = json.loads(render_requirement_traceability_json(artifact))
    report = render_requirement_traceability_markdown(artifact)

    assert all(row["status"] == "UNVERIFIED" for row in document["rows"])
    assert all(row["validation_links"] == [] for row in document["rows"])
    assert all(row["gaps"] for row in document["rows"])
    assert "UNVERIFIED — Implemented, validation not proven" in report
    assert "No qualifying governed validation is explicitly linked" in report
    assert "successful governed validation is not explicitly linked" in report
    assert "validation cannot be explicitly traced" in report
    assert "PYTHON_PYTEST" not in report


def test_success_writer_installs_both_reports_deterministically(
    tmp_path: Path,
    verified_state: WorkflowState,
) -> None:
    first_dir = tmp_path / "first"
    second_dir = tmp_path / "second"

    first_paths = write_artifacts(verified_state, first_dir)
    second_paths = write_artifacts(verified_state, second_dir)

    assert REQUIREMENT_TRACEABILITY_JSON_FILENAME in ARTIFACT_FILENAMES
    assert REQUIREMENT_TRACEABILITY_MARKDOWN_FILENAME in ARTIFACT_FILENAMES
    assert {path.name for path in first_paths} == set(ARTIFACT_FILENAMES)
    for filename in (
        REQUIREMENT_TRACEABILITY_JSON_FILENAME,
        REQUIREMENT_TRACEABILITY_MARKDOWN_FILENAME,
    ):
        assert (first_dir / filename).read_bytes() == (
            second_dir / filename
        ).read_bytes()
        assert not (first_dir / f".{filename}.tmp").exists()


def test_report_render_failure_leaves_no_partial_report_set(
    tmp_path: Path,
    verified_state: WorkflowState,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail_markdown(_artifact: object) -> str:
        raise RuntimeError("report rendering failed")

    monkeypatch.setattr(
        traceability_artifacts,
        "render_requirement_traceability_markdown",
        fail_markdown,
    )

    with pytest.raises(RuntimeError, match="report rendering failed"):
        write_requirement_traceability_artifacts(verified_state, tmp_path)

    assert not (tmp_path / REQUIREMENT_TRACEABILITY_JSON_FILENAME).exists()
    assert not (tmp_path / REQUIREMENT_TRACEABILITY_MARKDOWN_FILENAME).exists()


def test_greenfield_report_does_not_fabricate_brownfield_lineage(
    verified_state: WorkflowState,
) -> None:
    artifact = build_requirement_traceability_artifact(
        build_requirement_traceability(verified_state)
    )

    assert artifact.brownfield_lineage is None
    assert "## Brownfield Lineage" not in render_requirement_traceability_markdown(
        artifact
    )


def test_brownfield_reports_are_manifest_bound_and_published_byte_for_byte(
    brownfield_terminal,
) -> None:
    assert brownfield_terminal.manifest_path is not None
    assert brownfield_terminal.export_result is not None
    assert brownfield_terminal.export_result.succeeded
    assert brownfield_terminal.export_result.destination_directory is not None
    source = brownfield_terminal.artifact_bundle.artifact_dir
    published = (
        brownfield_terminal.export_result.destination_directory / "sdlc-artifacts"
    )
    manifest = json.loads(
        brownfield_terminal.manifest_path.read_text(encoding="utf-8")
    )
    records = {record["path"]: record for record in manifest["files"]}

    for filename in (
        REQUIREMENT_TRACEABILITY_JSON_FILENAME,
        REQUIREMENT_TRACEABILITY_MARKDOWN_FILENAME,
    ):
        source_bytes = (source / filename).read_bytes()
        published_bytes = (published / filename).read_bytes()
        assert source_bytes == published_bytes
        assert records[filename]["sha256"] == hashlib.sha256(source_bytes).hexdigest()
        assert records[filename]["size_bytes"] == len(source_bytes)

    document = json.loads(
        (source / REQUIREMENT_TRACEABILITY_JSON_FILENAME).read_text(encoding="utf-8")
    )
    report = (source / REQUIREMENT_TRACEABILITY_MARKDOWN_FILENAME).read_text(
        encoding="utf-8"
    )
    lineage = document["brownfield_lineage"]
    assert lineage["verified"] is True
    assert [step["stage"] for step in lineage["steps"]] == [
        "Selected baseline publication",
        "Baseline identity / integrity",
        "Bounded codebase context",
        "Approved impact analysis",
        "New requirement authority",
        "Approved TaskGraph",
        "Governed mutations / final snapshot",
    ]
    assert "New published project" not in report
    assert "publication has already happened" in report
    assert (
        "The approved impact analysis is traceable to the overall plan, but "
        "individual impact findings are not yet traceable to specific tasks."
        in report
    )
    assert "V0.16" not in report
    assert all(
        "impact" not in link["evidence_kind"].casefold()
        for row in document["rows"]
        for link in row["evidence_links"]
    )
    validation = brownfield_terminal.export_result.validation
    assert validation.evidence_source_valid
    assert validation.staged_evidence_matches
    assert validation.post_export_evidence_matches


def test_persisted_contract_rejects_post_publication_projection(
    brownfield_terminal,
) -> None:
    projection = build_requirement_traceability(
        brownfield_terminal.workflow_state,
        export_result=brownfield_terminal.export_result,
    )

    with pytest.raises(ValueError, match="before publication succeeds"):
        build_requirement_traceability_artifact(projection)
