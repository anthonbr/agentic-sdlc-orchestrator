"""Unit tests for canonical requirements and deterministic TaskGraph semantics."""

from __future__ import annotations

from pydantic import ValidationError
from pytest import mark, raises

from agentic_sdlc.requirement_analysis import RequirementAnalysis
from agentic_sdlc.requirement_spec import build_approved_requirement_spec
from agentic_sdlc.task_graph import (
    ProposedTask,
    ProposedTaskGraph,
    TaskGraphValidationError,
    TaskType,
    normalize_and_validate_task_graph,
)


FIXED_TIME = "2026-08-09T12:00:00+00:00"


def _analysis() -> RequirementAnalysis:
    return RequirementAnalysis(
        normalized_problem_statement="Build a governed URL-shortening service.",
        requirement_type="greenfield",
        functional_requirements=["Accept a long URL.", "Redirect a short URL."],
        nonfunctional_requirements=["Short identifiers must be unique."],
        constraints=["No storage technology has been selected."],
        ambiguities=["Whether shortened URLs expire is unspecified."],
        assumptions=[],
        acceptance_criteria=[
            "A valid URL receives a short URL.",
            "A known short URL redirects to its original URL.",
        ],
        risks=["Short-code collisions could redirect to the wrong URL."],
        needs_clarification=True,
        confidence=0.9,
    )


def _spec():
    return build_approved_requirement_spec(
        _analysis(), source_analysis_revision=2, created_at=FIXED_TIME
    )


def _task(
    key: str,
    *,
    depends_on: list[str] | None = None,
    requirement_refs: list[str] | None = None,
    acceptance_refs: list[str] | None = None,
    risk_refs: list[str] | None = None,
    ambiguity_refs: list[str] | None = None,
) -> ProposedTask:
    return ProposedTask(
        key=key,
        title=key.replace("_", " ").title(),
        description=f"Produce the {key} engineering definition.",
        task_type=TaskType.DESIGN,
        depends_on=depends_on if depends_on is not None else [],
        requirement_refs=(
            requirement_refs
            if requirement_refs is not None
            else ["FR-001", "FR-002", "NFR-001", "CON-001"]
        ),
        acceptance_criteria_refs=(
            acceptance_refs
            if acceptance_refs is not None
            else ["AC-001", "AC-002"]
        ),
        risk_refs=risk_refs if risk_refs is not None else [],
        ambiguity_refs=ambiguity_refs if ambiguity_refs is not None else [],
        expected_outputs=[f"{key}.md"],
    )


def _proposal(*tasks: ProposedTask) -> ProposedTaskGraph:
    return ProposedTaskGraph(tasks=list(tasks))


def test_approved_spec_assigns_namespaces_lineage_and_exact_text() -> None:
    spec = _spec()

    assert [item.item_id for item in spec.functional_requirements] == [
        "FR-001",
        "FR-002",
    ]
    assert spec.nonfunctional_requirements[0].item_id == "NFR-001"
    assert spec.constraints[0].item_id == "CON-001"
    assert spec.acceptance_criteria[0].item_id == "AC-001"
    assert spec.risks[0].item_id == "RISK-001"
    assert spec.ambiguities[0].item_id == "AMB-001"
    assert spec.functional_requirements[0].text == "Accept a long URL."
    assert spec.ambiguities[0].text == (
        "Whether shortened URLs expire is unspecified."
    )
    assert all(item.lineage_id for item in spec.all_items())
    assert spec.source_analysis_revision == 2
    assert spec.normalized_problem_statement == (
        "Build a governed URL-shortening service."
    )
    assert spec.requirement_type == "greenfield"
    assert spec.assumptions == ()
    assert spec.version == 1
    assert spec.created_at == FIXED_TIME
    assert spec.spec_id.startswith("SPEC-")
    assert len(spec.content_hash) == 64


def test_approved_spec_hash_and_ids_are_deterministic() -> None:
    first = _spec()
    second = _spec()

    assert first.spec_id == second.spec_id
    assert first.content_hash == second.content_hash
    assert [item.lineage_id for item in first.all_items()] == [
        item.lineage_id for item in second.all_items()
    ]


def test_duplicate_requirement_text_has_distinct_deterministic_lineage() -> None:
    analysis = _analysis().model_copy(
        update={"functional_requirements": ["Return an error.", "Return an error."]}
    )
    first = build_approved_requirement_spec(
        analysis, source_analysis_revision=0, created_at=FIXED_TIME
    )
    second = build_approved_requirement_spec(
        analysis, source_analysis_revision=0, created_at=FIXED_TIME
    )

    assert [item.item_id for item in first.functional_requirements] == [
        "FR-001",
        "FR-002",
    ]
    assert [item.text for item in first.functional_requirements] == [
        "Return an error.",
        "Return an error.",
    ]
    assert first.functional_requirements[0].lineage_id != (
        first.functional_requirements[1].lineage_id
    )
    assert [item.lineage_id for item in first.functional_requirements] == [
        item.lineage_id for item in second.functional_requirements
    ]


def test_spec_content_hash_includes_source_analysis_provenance() -> None:
    first = _spec()
    later_revision = build_approved_requirement_spec(
        _analysis(), source_analysis_revision=3, created_at=FIXED_TIME
    )

    assert later_revision.content_hash != first.content_hash


def test_approved_spec_version_has_unique_identity_and_stable_lineage() -> None:
    first = _spec()
    second = build_approved_requirement_spec(
        _analysis(),
        source_analysis_revision=2,
        version=2,
        supersedes_spec_id=first.spec_id,
        lineage_id=first.lineage_id,
        created_at=FIXED_TIME,
    )

    assert second.spec_id != first.spec_id
    assert second.spec_id.endswith("-V002")
    assert second.supersedes_spec_id == first.spec_id
    assert second.lineage_id == first.lineage_id
    assert second.content_hash == first.content_hash


def test_normalization_assigns_task_ids_and_remaps_dependencies() -> None:
    proposal = _proposal(
        _task("define_api", acceptance_refs=["AC-001"]),
        _task("implement_redirect", depends_on=["define_api"]),
    )

    graph, semantics = normalize_and_validate_task_graph(
        proposal, _spec(), version=1, created_at=FIXED_TIME
    )

    assert [task.task_id for task in graph.tasks] == ["TASK-001", "TASK-002"]
    assert graph.tasks[1].depends_on == ("TASK-001",)
    assert graph.tasks[0].source_key == "define_api"
    assert graph.tasks[0].lineage_id
    assert graph.graph_id.startswith("GRAPH-")
    assert graph.requirement_spec_id == _spec().spec_id
    assert semantics.topological_order == ("TASK-001", "TASK-002")
    assert semantics.execution_layers == (("TASK-001",), ("TASK-002",))
    assert semantics.entry_ready_tasks == ("TASK-001",)
    assert semantics.exit_predecessor_tasks == ("TASK-002",)


def test_task_graph_version_has_unique_identity_and_stable_lineage() -> None:
    proposal = _proposal(_task("define_api"))
    spec = _spec()
    first, _ = normalize_and_validate_task_graph(
        proposal, spec, version=1, created_at=FIXED_TIME
    )
    second, _ = normalize_and_validate_task_graph(
        proposal,
        spec,
        version=2,
        supersedes_graph_id=first.graph_id,
        graph_lineage_id=first.lineage_id,
        created_at=FIXED_TIME,
    )

    assert second.graph_id != first.graph_id
    assert second.graph_id.endswith("-V002")
    assert second.supersedes_graph_id == first.graph_id
    assert second.lineage_id == first.lineage_id
    assert second.content_hash == first.content_hash


def test_parallel_and_join_semantics_are_derived_from_dependencies() -> None:
    proposal = _proposal(
        _task("define_api"),
        _task("define_storage", requirement_refs=["CON-001"]),
        _task(
            "implement_service",
            depends_on=["define_api", "define_storage"],
            requirement_refs=["FR-001", "FR-002"],
            acceptance_refs=["AC-001", "AC-002"],
            risk_refs=["RISK-001"],
            ambiguity_refs=["AMB-001"],
        ),
    )

    _, semantics = normalize_and_validate_task_graph(
        proposal, _spec(), version=1, created_at=FIXED_TIME
    )

    assert semantics.execution_layers == (
        ("TASK-001", "TASK-002"),
        ("TASK-003",),
    )
    assert semantics.synchronization_points == ("TASK-003",)
    assert semantics.entry_ready_tasks == ("TASK-001", "TASK-002")
    assert semantics.exit_predecessor_tasks == ("TASK-003",)


def test_complete_core_specification_coverage_passes() -> None:
    graph, _ = normalize_and_validate_task_graph(
        _proposal(_task("cover_everything")),
        _spec(),
        version=1,
    )

    assert graph.tasks[0].requirement_refs == (
        "FR-001",
        "FR-002",
        "NFR-001",
        "CON-001",
    )
    assert graph.tasks[0].acceptance_criteria_refs == ("AC-001", "AC-002")


@mark.parametrize(
    ("requirement_refs", "acceptance_refs", "uncovered_id"),
    [
        (["FR-001", "NFR-001", "CON-001"], ["AC-001", "AC-002"], "FR-002"),
        (["FR-001", "FR-002", "CON-001"], ["AC-001", "AC-002"], "NFR-001"),
        (["FR-001", "FR-002", "NFR-001"], ["AC-001", "AC-002"], "CON-001"),
        (["FR-001", "FR-002", "NFR-001", "CON-001"], ["AC-001"], "AC-002"),
    ],
)
def test_missing_required_specification_coverage_is_rejected(
    requirement_refs: list[str],
    acceptance_refs: list[str],
    uncovered_id: str,
) -> None:
    with raises(TaskGraphValidationError, match=uncovered_id):
        normalize_and_validate_task_graph(
            _proposal(
                _task(
                    "incomplete",
                    requirement_refs=requirement_refs,
                    acceptance_refs=acceptance_refs,
                )
            ),
            _spec(),
            version=1,
        )


def test_multiple_uncovered_items_are_reported_in_namespace_order() -> None:
    with raises(TaskGraphValidationError) as captured:
        normalize_and_validate_task_graph(
            _proposal(
                _task(
                    "incomplete",
                    requirement_refs=["FR-001"],
                    acceptance_refs=[],
                )
            ),
            _spec(),
            version=1,
        )

    assert str(captured.value) == (
        "Uncovered approved specification items: "
        "FR-002, NFR-001, CON-001, AC-001, AC-002."
    )


def test_complete_risk_coverage_is_not_required() -> None:
    graph, _ = normalize_and_validate_task_graph(
        _proposal(_task("core_coverage", risk_refs=[])),
        _spec(),
        version=1,
    )

    assert graph.tasks[0].risk_refs == ()


def test_complete_ambiguity_coverage_is_not_required() -> None:
    graph, _ = normalize_and_validate_task_graph(
        _proposal(_task("core_coverage", ambiguity_refs=[])),
        _spec(),
        version=1,
    )

    assert graph.tasks[0].ambiguity_refs == ()


def test_duplicate_proposal_key_is_rejected() -> None:
    with raises(TaskGraphValidationError, match="keys must be unique"):
        normalize_and_validate_task_graph(
            _proposal(_task("same"), _task("same")),
            _spec(),
            version=1,
        )


def test_missing_dependency_is_rejected() -> None:
    with raises(TaskGraphValidationError, match="missing dependencies"):
        normalize_and_validate_task_graph(
            _proposal(_task("only_task", depends_on=["missing_task"])),
            _spec(),
            version=1,
        )


def test_self_dependency_is_rejected() -> None:
    with raises(TaskGraphValidationError, match="cannot depend on itself"):
        normalize_and_validate_task_graph(
            _proposal(_task("self_task", depends_on=["self_task"])),
            _spec(),
            version=1,
        )


def test_dependency_cycle_is_rejected() -> None:
    with raises(TaskGraphValidationError, match="no ENTRY-ready task"):
        normalize_and_validate_task_graph(
            _proposal(
                _task("first", depends_on=["second"]),
                _task("second", depends_on=["first"]),
            ),
            _spec(),
            version=1,
        )


@mark.parametrize("invalid_reference", ["FR-999", "NFR-999", "CON-999"])
def test_invalid_requirement_reference_is_rejected(invalid_reference: str) -> None:
    with raises(TaskGraphValidationError, match="requirement references"):
        normalize_and_validate_task_graph(
            _proposal(
                _task(
                    "task",
                    requirement_refs=[
                        "FR-001",
                        "FR-002",
                        "NFR-001",
                        "CON-001",
                        invalid_reference,
                    ],
                )
            ),
            _spec(),
            version=1,
        )


def test_invalid_acceptance_reference_is_rejected() -> None:
    with raises(TaskGraphValidationError, match="acceptance-criteria references"):
        normalize_and_validate_task_graph(
            _proposal(_task("task", acceptance_refs=["AC-999"])),
            _spec(),
            version=1,
        )


def test_invalid_risk_reference_is_rejected() -> None:
    with raises(TaskGraphValidationError, match="risk references"):
        normalize_and_validate_task_graph(
            _proposal(_task("task", risk_refs=["RISK-999"])),
            _spec(),
            version=1,
        )


def test_invalid_ambiguity_reference_is_rejected() -> None:
    with raises(TaskGraphValidationError, match="ambiguity references"):
        normalize_and_validate_task_graph(
            _proposal(_task("task", ambiguity_refs=["AMB-999"])),
            _spec(),
            version=1,
        )


def test_llm_cannot_supply_canonical_identity_fields() -> None:
    value = _task("define_api").model_dump(mode="json")
    value["task_id"] = "TASK-900"

    with raises(ValidationError):
        ProposedTask.model_validate(value)


def test_independent_root_task_is_connected_by_entry_and_exit_semantics() -> None:
    _, semantics = normalize_and_validate_task_graph(
        _proposal(_task("standalone")), _spec(), version=1
    )

    assert semantics.entry_ready_tasks == ("TASK-001",)
    assert semantics.exit_predecessor_tasks == ("TASK-001",)
