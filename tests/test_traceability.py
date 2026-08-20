"""Focused tests for deterministic reader-facing traceability projection."""

from __future__ import annotations

from copy import deepcopy

import pytest

from agentic_sdlc.application import GovernedRunMode, GovernedRunRequest
from agentic_sdlc.docker_validation import DockerPytestValidationExecutor
from agentic_sdlc.llm import FakeTaskPlanningClient
from agentic_sdlc.requirement_analysis import RequirementAnalysis
from agentic_sdlc.requirement_spec import build_approved_requirement_spec
from agentic_sdlc.state import WorkflowState, demo_input
from agentic_sdlc.task_graph import (
    ProposedTaskGraph,
    ProposedTaskValidationRequirement,
    TaskMaterializationPolicy,
    ValidationExecutionProfile,
)
from agentic_sdlc.traceability import (
    TraceabilityGapCode,
    TraceabilityItemKind,
    TraceabilityRelationshipBasis,
    TraceabilityStatus,
    build_requirement_traceability,
)
from agentic_sdlc.workspace_contracts import WorkspaceChangeOperation
from agentic_sdlc.workspace_integration import GovernedWorkspaceRuntime
from tests.test_application import _service
from tests.test_brownfield_baseline import _publish_project
from tests.test_brownfield_reasoning import _ContextAwareAnalyst
from tests.test_containerized_pytest_validation import ScriptedDockerRunner
from tests.test_containerized_pytest_workflow import _pytest_proposal
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
                "materialize_without_validation",
                depends_on=[],
                materialization_policy=TaskMaterializationPolicy.REQUIRED,
            )
        ]
    )
    return _run_approved(
        proposal,
        MaterializingExecutor({"TASK-001": "src/unverified.py"}),
    )


@pytest.fixture(scope="module")
def retry_state() -> WorkflowState:
    return _run_compile(
        ScriptedValidationExecutor({"TASK-001": ("fail", "pass")})
    )


@pytest.fixture(scope="module")
def multiple_task_state() -> WorkflowState:
    required = [
        ProposedTaskValidationRequirement(
            profile=ValidationExecutionProfile.PYTHON_COMPILE
        )
    ]
    proposal = ProposedTaskGraph(
        tasks=[
            _task(
                "first_implementation",
                depends_on=[],
                materialization_policy=TaskMaterializationPolicy.REQUIRED,
                required_validations=required,
            ),
            _task(
                "second_implementation",
                depends_on=[],
                materialization_policy=TaskMaterializationPolicy.REQUIRED,
                required_validations=required,
            ),
        ]
    )
    return _run_approved(
        proposal,
        MaterializingExecutor(
            {"TASK-001": "src/first.py", "TASK-002": "src/second.py"}
        ),
        validation_executor=ScriptedValidationExecutor(),
    )


@pytest.fixture(scope="module")
def no_change_state() -> WorkflowState:
    proposal = ProposedTaskGraph(
        tasks=[
            _task(
                "create_file",
                depends_on=[],
                materialization_policy=TaskMaterializationPolicy.REQUIRED,
            ),
            _task(
                "retain_file",
                depends_on=["create_file"],
                materialization_policy=TaskMaterializationPolicy.REQUIRED,
            ),
        ]
    )
    return _run_approved(
        proposal,
        MaterializingExecutor(
            {"TASK-001": "src/shared.py", "TASK-002": "src/shared.py"},
            contents={"TASK-001": "same\n", "TASK-002": "same\n"},
        ),
    )


@pytest.fixture(scope="module")
def pytest_state(tmp_path_factory: pytest.TempPathFactory) -> WorkflowState:
    root = tmp_path_factory.mktemp("traceability-pytest")
    runtime = GovernedWorkspaceRuntime(parent_directory=root)
    run_id = "traceability-pytest-run"
    live = runtime.establish_workspace_for_run(run_id)
    (live.root / "tests").mkdir()
    (live.root / "tests/test_placeholder.py").write_text(
        "def test_ok(): assert True\n"
    )
    return _run_approved(
        _pytest_proposal(),
        MaterializingExecutor({"TASK-001": "src/candidate.py"}),
        workspace_runtime=runtime,
        thread_id=run_id,
        validation_executor=DockerPytestValidationExecutor(
            runner=ScriptedDockerRunner(),
            docker_executable="/application/docker",
        ),
    )


@pytest.fixture(scope="module")
def brownfield_terminal(tmp_path_factory: pytest.TempPathFactory):
    root = tmp_path_factory.mktemp("traceability-brownfield")
    _publish_project(root)
    service, _, _ = _service(
        root,
        analyst=_ContextAwareAnalyst(blocked_revisions=(False,)),
        planner=FakeTaskPlanningClient([_runnable_proposal()]),
        run_suffix="traceability-brownfield",
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


def test_greenfield_happy_path_is_verified(verified_state: WorkflowState) -> None:
    projection = build_requirement_traceability(verified_state)

    assert projection.brownfield_lineage is None
    assert [row.status for row in projection.rows] == [
        TraceabilityStatus.VERIFIED,
        TraceabilityStatus.VERIFIED,
    ]
    assert all(row.validation_links[0].outcome == "PASSED" for row in projection.rows)
    assert all(not row.gaps for row in projection.rows)


def test_multiple_tasks_can_cover_one_acceptance_criterion(
    multiple_task_state: WorkflowState,
) -> None:
    projection = build_requirement_traceability(multiple_task_state)
    row = next(item for item in projection.rows if item.item_id == "AC-001")

    assert [item.task_id for item in row.task_links] == ["TASK-001", "TASK-002"]
    assert [item.target_path for item in row.implementation_links] == [
        "src/first.py",
        "src/second.py",
    ]
    assert len(row.validation_links) == 2


def test_one_task_can_cover_multiple_canonical_items(
    verified_state: WorkflowState,
) -> None:
    projection = build_requirement_traceability(verified_state)

    assert [(row.item_id, row.task_links[0].task_id) for row in projection.rows] == [
        ("FR-001", "TASK-001"),
        ("AC-001", "TASK-001"),
    ]


def test_implementation_without_governed_validation_is_unverified(
    unverified_state: WorkflowState,
) -> None:
    row = build_requirement_traceability(unverified_state).rows[0]

    assert row.status is TraceabilityStatus.UNVERIFIED
    assert row.validation_links == ()
    assert any(gap.code is TraceabilityGapCode.GOVERNED_VALIDATION for gap in row.gaps)


def test_no_materialized_implementation_is_not_implemented() -> None:
    state = _run_approved(_single_proposal(), DeterministicExecutor())
    projection = build_requirement_traceability(state)

    assert all(
        row.status is TraceabilityStatus.NOT_IMPLEMENTED for row in projection.rows
    )
    assert all(row.artifact_links for row in projection.rows)
    assert all(not row.implementation_links for row in projection.rows)


def test_legitimate_no_change_remains_visible_lineage(
    no_change_state: WorkflowState,
) -> None:
    row = build_requirement_traceability(no_change_state).rows[0]

    assert [item.operation for item in row.implementation_links] == [
        WorkspaceChangeOperation.CREATE,
        WorkspaceChangeOperation.NO_CHANGE,
    ]
    assert row.status is TraceabilityStatus.UNVERIFIED


def test_failed_superseded_attempt_is_ignored(retry_state: WorkflowState) -> None:
    projection = build_requirement_traceability(retry_state)
    row = projection.rows[0]

    assert len(row.validation_links) == 1
    assert row.validation_links[0].evidence_id == (
        retry_state["task_validation_execution_evidence"][-1].evidence_id
    )
    assert {link.attempt_id for link in row.artifact_links} == {
        retry_state["task_execution_requests"][-1].attempt_id
    }


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("task_id", "TASK-999"),
        ("request_id", "wrong-request"),
        ("attempt_id", "wrong-attempt"),
        ("source_workspace_id", "wrong-workspace"),
        ("graph_id", "wrong-graph"),
        ("policy_id", "wrong-policy"),
    ),
)
def test_mismatched_validation_evidence_is_ignored(
    verified_state: WorkflowState,
    field: str,
    value: str,
) -> None:
    state = deepcopy(verified_state)
    evidence = state["task_validation_execution_evidence"][0]
    state["task_validation_execution_evidence"] = [
        evidence.model_copy(update={field: value})
    ]

    row = build_requirement_traceability(state).rows[0]

    assert row.validation_links == ()
    assert row.status is TraceabilityStatus.UNVERIFIED


def test_semantic_artifact_logical_name_is_never_used_as_target_path(
    verified_state: WorkflowState,
) -> None:
    row = build_requirement_traceability(verified_state).rows[0]

    assert row.artifact_links[0].logical_name == "semantic-TASK-001"
    assert row.implementation_links[0].target_path == "src/candidate.py"
    assert row.artifact_links[0].logical_name != row.implementation_links[0].target_path


def test_test_artifact_does_not_imply_governed_validation(
    unverified_state: WorkflowState,
) -> None:
    row = build_requirement_traceability(unverified_state).rows[0]

    assert row.artifact_links[0].artifact_type == "TEST"
    assert row.validation_links == ()
    assert row.status is TraceabilityStatus.UNVERIFIED


def test_compile_pass_remains_explicitly_compile_evidence(
    verified_state: WorkflowState,
) -> None:
    row = build_requirement_traceability(verified_state).rows[0]

    assert [link.profile for link in row.validation_links] == [
        ValidationExecutionProfile.PYTHON_COMPILE
    ]
    assert "PYTHON_COMPILE PASS" in row.status_reason
    assert "pytest" not in row.status_reason.casefold()


def test_pytest_pass_retains_profile_and_provisioning_basis(
    pytest_state: WorkflowState,
) -> None:
    row = build_requirement_traceability(pytest_state).rows[0]

    assert row.status is TraceabilityStatus.VERIFIED
    assert row.validation_links[0].profile is ValidationExecutionProfile.PYTHON_PYTEST
    assert len(row.validation_links[0].provisioning_evidence_ids) == 1


def test_missing_task_graph_links_are_visible_and_deterministically_ordered() -> None:
    analysis = RequirementAnalysis(
        normalized_problem_statement="Cover every canonical namespace.",
        requirement_type="greenfield",
        functional_requirements=["Functional one.", "Functional two."],
        nonfunctional_requirements=["Reliable behavior."],
        constraints=["Use the governed workspace."],
        ambiguities=[],
        assumptions=[],
        acceptance_criteria=["Outcome one.", "Outcome two."],
        risks=[],
        needs_clarification=False,
        confidence=1.0,
    )
    spec = build_approved_requirement_spec(
        analysis,
        source_analysis_revision=3,
        created_at="2026-08-15T12:00:00+00:00",
    )
    projection = build_requirement_traceability(
        {
            "run_id": "incomplete-run",
            "approved_requirement_spec": spec.model_dump(mode="json"),
        }
    )

    assert [row.item_id for row in projection.rows] == [
        "FR-001",
        "FR-002",
        "NFR-001",
        "CON-001",
        "AC-001",
        "AC-002",
    ]
    assert [row.item_kind for row in projection.rows] == [
        TraceabilityItemKind.FUNCTIONAL_REQUIREMENT,
        TraceabilityItemKind.FUNCTIONAL_REQUIREMENT,
        TraceabilityItemKind.NONFUNCTIONAL_REQUIREMENT,
        TraceabilityItemKind.CONSTRAINT,
        TraceabilityItemKind.ACCEPTANCE_CRITERION,
        TraceabilityItemKind.ACCEPTANCE_CRITERION,
    ]
    assert len({row.item_id for row in projection.rows}) == len(projection.rows)
    assert all(row.status is TraceabilityStatus.NOT_IMPLEMENTED for row in projection.rows)
    assert all(any(gap.code is TraceabilityGapCode.TASK_LINK for gap in row.gaps) for row in projection.rows)


def test_tampered_mutation_identity_cannot_establish_implementation(
    verified_state: WorkflowState,
) -> None:
    state = deepcopy(verified_state)
    mutation = state["workspace_mutation_results"][0]
    state["workspace_mutation_results"] = [
        mutation.model_copy(update={"mutation_id": "WORKSPACE-MUTATION-TAMPERED"})
    ]

    row = build_requirement_traceability(state).rows[0]

    assert row.implementation_links == ()
    assert row.status is TraceabilityStatus.NOT_IMPLEMENTED


def test_incomplete_final_run_authority_fails_closed(
    verified_state: WorkflowState,
) -> None:
    state = deepcopy(verified_state)
    state["exit_gate_passed"] = False

    row = build_requirement_traceability(state).rows[0]

    assert row.implementation_links
    assert row.validation_links
    assert row.status is TraceabilityStatus.UNVERIFIED
    assert any(gap.code is TraceabilityGapCode.FINAL_RUN_AUTHORITY for gap in row.gaps)


def test_valid_brownfield_lineage_reaches_new_publication(
    brownfield_terminal,
) -> None:
    projection = build_requirement_traceability(
        brownfield_terminal.workflow_state,
        export_result=brownfield_terminal.export_result,
    )
    lineage = projection.brownfield_lineage

    assert lineage is not None and lineage.verified
    assert lineage.gaps == ()
    assert [step.stage for step in lineage.steps] == [
        "Selected baseline publication",
        "Baseline identity / integrity",
        "Bounded codebase context",
        "Approved impact analysis",
        "New requirement authority",
        "Approved TaskGraph",
        "Governed mutations / final snapshot",
        "New published project",
    ]
    assert lineage.steps[0].identity == "published-project"
    assert lineage.steps[-1].identity == "enhanced-project"


def test_valid_brownfield_lineage_stops_at_final_snapshot_before_publication(
    brownfield_terminal,
) -> None:
    projection = build_requirement_traceability(brownfield_terminal.workflow_state)
    lineage = projection.brownfield_lineage

    assert lineage is not None and lineage.verified
    assert lineage.gaps == ()
    assert [step.stage for step in lineage.steps] == [
        "Selected baseline publication",
        "Baseline identity / integrity",
        "Bounded codebase context",
        "Approved impact analysis",
        "New requirement authority",
        "Approved TaskGraph",
        "Governed mutations / final snapshot",
    ]
    assert projection.final_authority.publication_succeeded is False
    assert projection.final_authority.publication_project_name is None


def test_prepublication_brownfield_lineage_requires_complete_final_authority(
    brownfield_terminal,
) -> None:
    state = {**brownfield_terminal.workflow_state, "exit_gate_passed": False}

    lineage = build_requirement_traceability(state).brownfield_lineage

    assert lineage is not None
    assert lineage.verified is False
    assert lineage.steps == ()
    assert any(
        gap.code is TraceabilityGapCode.FINAL_RUN_AUTHORITY
        for gap in lineage.gaps
    )


def test_malformed_brownfield_correlation_fails_closed(
    brownfield_terminal,
) -> None:
    context = dict(brownfield_terminal.workflow_state["brownfield_codebase_context"])
    context["baseline_id"] = "stale-baseline"
    state = {
        **brownfield_terminal.workflow_state,
        "brownfield_codebase_context": context,
    }

    lineage = build_requirement_traceability(
        state,
        export_result=brownfield_terminal.export_result,
    ).brownfield_lineage

    assert lineage is not None
    assert lineage.verified is False
    assert lineage.steps == ()
    assert lineage.gaps[0].code is TraceabilityGapCode.BROWNFIELD_CORRELATION


def test_greenfield_does_not_fabricate_brownfield_lineage(
    verified_state: WorkflowState,
) -> None:
    assert build_requirement_traceability(verified_state).brownfield_lineage is None


def test_impact_analysis_stays_run_level_without_finding_to_task_edges(
    brownfield_terminal,
) -> None:
    projection = build_requirement_traceability(
        brownfield_terminal.workflow_state,
        export_result=brownfield_terminal.export_result,
    )
    lineage = projection.brownfield_lineage

    assert lineage is not None and lineage.verified
    impact_step = next(
        step for step in lineage.steps if step.stage == "Approved impact analysis"
    )
    assert impact_step.basis is TraceabilityRelationshipBasis.EXPLICIT
    assert all("impact" not in link.evidence_kind.casefold() for row in projection.rows for link in row.evidence_links)


def test_projection_is_read_only(verified_state: WorkflowState) -> None:
    before = deepcopy(verified_state)

    first = build_requirement_traceability(verified_state)
    second = build_requirement_traceability(verified_state)

    assert verified_state == before
    assert first == second
