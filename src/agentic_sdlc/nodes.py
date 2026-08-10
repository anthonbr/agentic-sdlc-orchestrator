"""Governed planning and execution nodes for the static control plane."""

from __future__ import annotations

import json
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import cast

from langgraph.types import interrupt
from pydantic import ValidationError

from agentic_sdlc.llm import (
    RequirementAnalysisClient,
    RequirementAnalysisClientError,
    TaskPlanningClient,
    TaskPlanningClientError,
)
from agentic_sdlc.prompts import (
    REQUIREMENT_ANALYSIS_PROMPT_VERSION,
    TASK_PLANNING_PROMPT_VERSION,
)
from agentic_sdlc.requirement_analysis import RequirementAnalysis
from agentic_sdlc.requirement_spec import (
    ApprovedRequirementSpec,
    build_approved_requirement_spec as package_approved_requirement_spec,
)
from agentic_sdlc.state import (
    MAX_REQUIREMENT_REVISIONS_REASON,
    MAX_TASK_GRAPH_REVISIONS_REASON,
    REQUIREMENT_ANALYSIS_ATTEMPTS_REASON,
    REQUIREMENT_ANALYSIS_REJECTED_REASON,
    TASK_GRAPH_REJECTED_REASON,
    TASK_PLANNING_ATTEMPTS_REASON,
    ApprovalDecision,
    ApprovalEvent,
    ApprovalResponse,
    ApprovedRequirementSpecData,
    RequirementAnalysisData,
    RequirementAnalysisFailure,
    RequirementAnalysisRecord,
    TaskGraphData,
    TaskGraphRecord,
    TaskGraphSemanticsData,
    TaskPlanningFailure,
    WorkflowState,
)
from agentic_sdlc.task_graph import (
    ProposedTaskGraph,
    TaskGraph,
    TaskGraphValidationError,
    normalize_and_validate_task_graph as normalize_task_graph,
)
from agentic_sdlc.task_execution import (
    MAX_PARALLEL_TASK_EXECUTIONS,
    TaskExecutionError,
    TaskExecutionFailure,
    TaskExecutionFailurePhase,
    TaskExecutionRecoveryAction,
    TaskExecutionRecoveryDecision,
    TaskExecutionRecoveryFailureKind,
    TaskExecutionStatus,
    TaskExecutionWave,
    TaskExecutionWaveAttempt,
    TaskGraphExecutionState,
    TaskGraphExecutionStatus,
    decide_task_execution_recovery,
    initialize_task_graph_execution,
    mark_task_failed,
    mark_task_succeeded,
    prepare_task_retry,
    ready_task_wave_ids,
    safe_stop_task_graph_execution,
    start_task_wave,
)
from agentic_sdlc.task_execution_contracts import (
    EngineeringArtifact,
    TaskExecutionContractError,
    TaskExecutionCorrelationError,
    TaskExecutionRequest,
    TaskExecutionResult,
    TaskExecutionValidationResult,
    build_task_execution_request,
    canonicalize_execution_result,
    classify_validation_failure,
    validate_execution_result,
)
from agentic_sdlc.task_executor import TaskExecutor, TaskExecutorError


def requirements_intake(state: WorkflowState) -> WorkflowState:
    """Preserve submitted requirements and initialize governed workflow state."""

    original_requirements = list(state.get("requirements", []))
    normalized_texts = [
        requirement.strip()
        for requirement in original_requirements
        if requirement.strip()
    ]
    normalized_requirements = [
        {"id": f"REQ-{index:03d}", "text": text}
        for index, text in enumerate(normalized_texts, start=1)
    ]
    raw_requirement = state.get("raw_requirement", "").strip()
    if not raw_requirement:
        raw_requirement = "\n".join(normalized_texts)

    return {
        "project_name": state.get("project_name", "").strip(),
        "requirements": original_requirements,
        "raw_requirement": raw_requirement,
        "normalized_requirements": normalized_requirements,
        "entry_gate_passed": False,
        "requirement_analysis_candidate": None,
        "requirement_analysis_status": "pending",
        "requirement_analysis_attempt_count": 0,
        "requirement_analysis_retryable": True,
        "requirement_analysis_error": "",
        "requirement_analysis_revision_count": 0,
        "requirement_analysis_model": "",
        "requirement_analysis_history": [],
        "requirement_analysis_failures": [],
        "requirement_review_decision": None,
        "requirement_review_feedback": "",
        "requirement_review_history": [],
        "task_planning_candidate": None,
        "task_planning_status": "pending",
        "task_planning_attempt_count": 0,
        "task_planning_retryable": True,
        "task_planning_error": "",
        "task_graph_revision_count": 0,
        "task_planning_model": "",
        "task_graph_history": [],
        "task_planning_failures": [],
        "task_graph_decision": None,
        "task_graph_feedback": "",
        "task_graph_review_history": [],
        "safe_stop_reason": "",
        "exit_gate_passed": False,
        "workflow_status": "pending",
        "errors": [],
        "trace": ["[requirements_intake] complete"],
    }


def entry_gate(state: WorkflowState) -> WorkflowState:
    """Reject inputs that cannot support meaningful downstream work."""

    problems: list[str] = []
    if not state.get("project_name", "").strip():
        problems.append("A non-empty project name is required.")
    if not state.get("normalized_requirements"):
        problems.append("At least one non-empty requirement is required.")
    if problems:
        return {
            "entry_gate_passed": False,
            "workflow_status": "entry_gate_failed",
            "errors": problems,
            "trace": ["[entry_gate] failed"],
        }
    return {"entry_gate_passed": True, "trace": ["[entry_gate] passed"]}


def requirement_analysis_task(
    state: WorkflowState,
    *,
    client: RequirementAnalysisClient,
) -> WorkflowState:
    """Ask the injected analyst for one structured requirement candidate."""

    attempt_number = state.get("requirement_analysis_attempt_count", 0) + 1
    prior_analysis = None
    if state.get("requirement_analysis"):
        prior_analysis = RequirementAnalysis.model_validate(
            state["requirement_analysis"]
        )
    try:
        candidate = client.invoke_structured(
            state["raw_requirement"],
            prior_analysis,
            state.get("requirement_review_feedback", ""),
        )
    except RequirementAnalysisClientError as error:
        failure = _requirement_analysis_failure(
            state,
            attempt_number=attempt_number,
            reason=str(error),
            retryable=error.retryable,
        )
        return {
            "requirement_analysis_candidate": None,
            "requirement_analysis_status": "failed",
            "requirement_analysis_attempt_count": attempt_number,
            "requirement_analysis_retryable": error.retryable,
            "requirement_analysis_error": str(error),
            "requirement_analysis_model": client.model_name,
            "requirement_analysis_failures": [failure],
            "trace": [f"[requirement_analysis_task] attempt {attempt_number} failed"],
        }
    if isinstance(candidate, RequirementAnalysis):
        candidate = candidate.model_dump(mode="json")
    return {
        "requirement_analysis_candidate": candidate,
        "requirement_analysis_status": "candidate",
        "requirement_analysis_attempt_count": attempt_number,
        "requirement_analysis_retryable": True,
        "requirement_analysis_error": "",
        "requirement_analysis_model": client.model_name,
        "trace": [f"[requirement_analysis_task] attempt {attempt_number} complete"],
    }


def validate_requirement_analysis(state: WorkflowState) -> WorkflowState:
    """Validate one LLM candidate before it can reach human review."""

    try:
        analysis = RequirementAnalysis.model_validate(
            state.get("requirement_analysis_candidate")
        )
    except ValidationError as error:
        reason = _pydantic_failure_reason(
            "Structured requirement analysis validation", error
        )
        failure = _requirement_analysis_failure(
            state,
            attempt_number=state["requirement_analysis_attempt_count"],
            reason=reason,
            retryable=True,
        )
        return {
            "requirement_analysis_candidate": None,
            "requirement_analysis_status": "failed",
            "requirement_analysis_retryable": True,
            "requirement_analysis_error": reason,
            "requirement_analysis_failures": [failure],
            "trace": ["[validate_requirement_analysis] failed"],
        }
    analysis_data = cast(RequirementAnalysisData, analysis.model_dump(mode="json"))
    record: RequirementAnalysisRecord = {
        "sequence": len(state.get("requirement_analysis_history", [])) + 1,
        "revision_number": state.get("requirement_analysis_revision_count", 0),
        "attempt_number": state["requirement_analysis_attempt_count"],
        "prompt_version": REQUIREMENT_ANALYSIS_PROMPT_VERSION,
        "model_name": state["requirement_analysis_model"],
        "reviewer_feedback": state.get("requirement_review_feedback", ""),
        "analysis": analysis_data,
    }
    return {
        "requirement_analysis_candidate": None,
        "requirement_analysis": analysis_data,
        "requirement_analysis_status": "validated",
        "requirement_analysis_retryable": False,
        "requirement_analysis_error": "",
        "requirement_analysis_history": [record],
        "workflow_status": "awaiting_approval",
        "trace": ["[validate_requirement_analysis] passed"],
    }


def prepare_requirement_analysis_retry(state: WorkflowState) -> WorkflowState:
    """Prepare a machine retry without changing human revision lineage."""

    next_attempt = state["requirement_analysis_attempt_count"] + 1
    return {
        "requirement_analysis_candidate": None,
        "requirement_analysis_status": "pending",
        "trace": [f"[requirement_analysis_retry] preparing attempt {next_attempt}"],
    }


def requirement_analysis_review(state: WorkflowState) -> WorkflowState:
    """Pause for human authority over one validated requirement analysis."""

    response = cast(
        ApprovalResponse,
        interrupt(
            {
                "stage": "requirement_analysis_review",
                "checkpoint": "requirement_analysis",
                "message": "Requirement analysis requires human review.",
                "requirement_analysis": state["requirement_analysis"],
                "revision_number": state.get(
                    "requirement_analysis_revision_count", 0
                ),
                "allowed_decisions": ["APPROVE", "REQUEST_CHANGES", "REJECT"],
            }
        ),
    )
    decision, feedback = _validated_approval_response(
        response, checkpoint="requirement-analysis"
    )
    revision_number = state.get("requirement_analysis_revision_count", 0)
    event: ApprovalEvent = {
        "sequence": len(state.get("requirement_review_history", [])) + 1,
        "checkpoint": "requirement_analysis",
        "decision": decision,
        "feedback": feedback,
        "revision_number": revision_number,
    }
    return {
        "requirement_review_decision": decision,
        "requirement_review_feedback": feedback,
        "requirement_review_history": [event],
        "workflow_status": "pending",
        "trace": [f"[requirement_analysis_review] {decision.lower()}"],
    }


def prepare_requirement_analysis_revision(state: WorkflowState) -> WorkflowState:
    """Start a human-requested analysis revision with a fresh retry budget."""

    revision_number = state.get("requirement_analysis_revision_count", 0) + 1
    return {
        "requirement_analysis_candidate": None,
        "requirement_analysis_status": "pending",
        "requirement_analysis_attempt_count": 0,
        "requirement_analysis_retryable": True,
        "requirement_analysis_error": "",
        "requirement_analysis_revision_count": revision_number,
        "requirement_review_decision": None,
        "trace": [
            f"[prepare_requirement_analysis_revision] revision {revision_number}"
        ],
    }


def build_approved_requirement_spec(state: WorkflowState) -> WorkflowState:
    """Deterministically package the exact analysis approved by the human."""

    if state.get("requirement_review_decision") != "APPROVE":
        raise ValueError("Requirement specification requires human approval.")
    analysis = RequirementAnalysis.model_validate(state["requirement_analysis"])
    spec = package_approved_requirement_spec(
        analysis,
        source_analysis_revision=state.get("requirement_analysis_revision_count", 0),
    )
    return {
        "approved_requirement_spec": cast(
            ApprovedRequirementSpecData, spec.model_dump(mode="json")
        ),
        "trace": [
            f"[build_approved_requirement_spec] {spec.spec_id} version {spec.version}"
        ],
    }


def task_decomposition_task(
    state: WorkflowState,
    *,
    client: TaskPlanningClient,
) -> WorkflowState:
    """Ask the injected planner for one semantic task dependency proposal."""

    attempt_number = state.get("task_planning_attempt_count", 0) + 1
    spec = _spec_from_state(state)
    prior_graph = None
    if state.get("candidate_task_graph"):
        prior_graph = _task_graph_from_data(state["candidate_task_graph"])
    try:
        candidate = client.invoke_structured(
            spec, prior_graph, state.get("task_graph_feedback", "")
        )
    except TaskPlanningClientError as error:
        failure = _task_planning_failure(
            state,
            attempt_number=attempt_number,
            reason=str(error),
            retryable=error.retryable,
        )
        return {
            "task_planning_candidate": None,
            "task_planning_status": "failed",
            "task_planning_attempt_count": attempt_number,
            "task_planning_retryable": error.retryable,
            "task_planning_error": str(error),
            "task_planning_model": client.model_name,
            "task_planning_failures": [failure],
            "trace": [f"[task_decomposition_task] attempt {attempt_number} failed"],
        }
    if isinstance(candidate, ProposedTaskGraph):
        candidate = candidate.model_dump(mode="json")
    return {
        "task_planning_candidate": candidate,
        "task_planning_status": "candidate",
        "task_planning_attempt_count": attempt_number,
        "task_planning_retryable": True,
        "task_planning_error": "",
        "task_planning_model": client.model_name,
        "trace": [f"[task_decomposition_task] attempt {attempt_number} complete"],
    }


def normalize_and_validate_task_graph(state: WorkflowState) -> WorkflowState:
    """Validate the proposal, assign authority, and derive graph semantics."""

    try:
        proposal = ProposedTaskGraph.model_validate_json(
            json.dumps(state.get("task_planning_candidate"))
        )
        spec = _spec_from_state(state)
        previous_graph = None
        if state.get("candidate_task_graph"):
            previous_graph = _task_graph_from_data(state["candidate_task_graph"])
        graph, semantics = normalize_task_graph(
            proposal,
            spec,
            version=state.get("task_graph_revision_count", 0) + 1,
            supersedes_graph_id=(
                previous_graph.graph_id if previous_graph is not None else None
            ),
            graph_lineage_id=(
                previous_graph.lineage_id if previous_graph is not None else None
            ),
        )
    except ValidationError as error:
        reason = _pydantic_failure_reason("Structured task proposal validation", error)
        return _failed_task_graph_validation(state, reason)
    except TaskGraphValidationError as error:
        return _failed_task_graph_validation(state, str(error))

    graph_data = cast(TaskGraphData, graph.model_dump(mode="json"))
    semantics_data = cast(
        TaskGraphSemanticsData, semantics.model_dump(mode="json")
    )
    record: TaskGraphRecord = {
        "sequence": len(state.get("task_graph_history", [])) + 1,
        "revision_number": state.get("task_graph_revision_count", 0),
        "attempt_number": state["task_planning_attempt_count"],
        "prompt_version": TASK_PLANNING_PROMPT_VERSION,
        "model_name": state["task_planning_model"],
        "reviewer_feedback": state.get("task_graph_feedback", ""),
        "task_graph": graph_data,
    }
    return {
        "task_planning_candidate": None,
        "task_planning_status": "validated",
        "task_planning_retryable": False,
        "task_planning_error": "",
        "candidate_task_graph": graph_data,
        "task_graph_semantics": semantics_data,
        "task_graph_history": [record],
        "workflow_status": "awaiting_approval",
        "trace": [
            f"[normalize_and_validate_task_graph] {graph.graph_id} passed; "
            "FR/NFR/CON/AC coverage complete"
        ],
    }


def prepare_task_planning_retry(state: WorkflowState) -> WorkflowState:
    """Prepare another machine attempt for the same task-graph revision."""

    next_attempt = state["task_planning_attempt_count"] + 1
    return {
        "task_planning_candidate": None,
        "task_planning_status": "pending",
        "trace": [f"[task_planning_retry] preparing attempt {next_attempt}"],
    }


def task_graph_review(state: WorkflowState) -> WorkflowState:
    """Pause for human authority over a deterministically validated TaskGraph."""

    response = cast(
        ApprovalResponse,
        interrupt(
            {
                "stage": "task_graph_review",
                "checkpoint": "task_graph",
                "message": "Engineering task graph requires human review.",
                "approved_requirement_spec": state["approved_requirement_spec"],
                "candidate_task_graph": state["candidate_task_graph"],
                "graph_semantics": state["task_graph_semantics"],
                "revision_number": state.get("task_graph_revision_count", 0),
                "allowed_decisions": ["APPROVE", "REQUEST_CHANGES", "REJECT"],
            }
        ),
    )
    decision, feedback = _validated_approval_response(
        response, checkpoint="task-graph"
    )
    revision_number = state.get("task_graph_revision_count", 0)
    event: ApprovalEvent = {
        "sequence": len(state.get("task_graph_review_history", [])) + 1,
        "checkpoint": "task_graph",
        "decision": decision,
        "feedback": feedback,
        "revision_number": revision_number,
    }
    return {
        "task_graph_decision": decision,
        "task_graph_feedback": feedback,
        "task_graph_review_history": [event],
        "workflow_status": "pending",
        "trace": [f"[task_graph_review] {decision.lower()}"],
    }


def prepare_task_graph_revision(state: WorkflowState) -> WorkflowState:
    """Start a human-requested graph revision with a fresh machine budget."""

    revision_number = state.get("task_graph_revision_count", 0) + 1
    return {
        "task_planning_candidate": None,
        "task_planning_status": "pending",
        "task_planning_attempt_count": 0,
        "task_planning_retryable": True,
        "task_planning_error": "",
        "task_graph_revision_count": revision_number,
        "task_graph_decision": None,
        "trace": [f"[prepare_task_graph_revision] revision {revision_number}"],
    }


def approve_task_graph(state: WorkflowState) -> WorkflowState:
    """Promote the reviewed candidate to the authoritative plan for this run."""

    if state.get("task_graph_decision") != "APPROVE":
        raise ValueError("Task graph promotion requires human approval.")
    graph = _task_graph_from_data(state["candidate_task_graph"])
    return {
        "approved_task_graph": cast(TaskGraphData, graph.model_dump(mode="json")),
        "workflow_status": "pending",
        "trace": [f"[approve_task_graph] {graph.graph_id} approved"],
    }


def initialize_task_graph_execution_node(state: WorkflowState) -> WorkflowState:
    """Initialize runtime state only for the human-approved canonical graph."""

    if state.get("task_graph_decision") != "APPROVE" or not state.get(
        "approved_task_graph"
    ):
        raise ValueError("TaskGraph execution requires an approved TaskGraph.")
    graph = _task_graph_from_data(state["approved_task_graph"])
    execution = initialize_task_graph_execution(graph)
    return {
        "task_graph_execution": execution,
        "workflow_status": "pending",
        "trace": [f"[initialize_task_graph_execution] {graph.graph_id} pending"],
    }


@dataclass
class _WaveAttemptRecord:
    """Ephemeral single-threaded orchestration state for one wave member."""

    task_id: str
    attempt_number: int
    request: TaskExecutionRequest | None = None
    result: TaskExecutionResult | None = None
    artifacts: tuple[EngineeringArtifact, ...] = ()
    validation: TaskExecutionValidationResult | None = None
    failure: TaskExecutionFailure | None = None
    recovery_decision: TaskExecutionRecoveryDecision | None = None
    succeeded: bool = False


def execute_task_graph_step(
    state: WorkflowState,
    *,
    executor: TaskExecutor,
) -> WorkflowState:
    """Execute and settle one bounded deterministic READY wave."""

    spec = _spec_from_state(state)
    graph = _task_graph_from_data(state["approved_task_graph"])
    execution = _execution_from_state(state["task_graph_execution"])
    wave_task_ids = ready_task_wave_ids(graph, execution)
    if not wave_task_ids:
        raise TaskExecutionError(
            "A nonterminal TaskGraph execution has no READY task to dispatch."
        )

    started = start_task_wave(graph, execution, wave_task_ids)
    runtime_by_task = {item.task_id: item for item in started.task_states}
    wave_number = _next_task_execution_wave_number(state)
    wave = TaskExecutionWave(
        wave_number=wave_number,
        task_attempts=tuple(
            TaskExecutionWaveAttempt(
                task_id=task_id,
                attempt_number=runtime_by_task[task_id].attempt_count,
            )
            for task_id in wave_task_ids
        ),
    )
    records = [
        _WaveAttemptRecord(
            task_id=attempt.task_id,
            attempt_number=attempt.attempt_number,
        )
        for attempt in wave.task_attempts
    ]

    # Request construction is deliberately sequential and canonical.
    for record in records:
        try:
            artifacts, validations = _dependency_evidence(
                state, graph, started, record.task_id
            )
            prior_recovery = _prior_recovery_decision(
                state, record.task_id, record.attempt_number
            )
            record.request = build_task_execution_request(
                spec,
                graph,
                started,
                record.task_id,
                artifacts,
                validations,
                prior_recovery_decision=prior_recovery,
            )
        except TaskExecutionContractError as error:
            record.failure, record.recovery_decision = (
                _classify_non_validation_failure(
                    task_id=record.task_id,
                    attempt_number=record.attempt_number,
                    phase=TaskExecutionFailurePhase.REQUEST_BUILD,
                    failure_kind=TaskExecutionRecoveryFailureKind.REQUEST_BUILD,
                    retryable=False,
                    error=error,
                )
            )

    # Worker threads invoke only the injected executor. Collection and every
    # application-owned operation remain on this orchestration thread.
    futures: dict[str, Future[TaskExecutionResult]] = {}
    with ThreadPoolExecutor(
        max_workers=MAX_PARALLEL_TASK_EXECUTIONS,
        thread_name_prefix="task-executor",
    ) as pool:
        for record in records:
            if record.request is not None:
                futures[record.task_id] = pool.submit(
                    executor.execute, record.request
                )
        for record in records:
            future = futures.get(record.task_id)
            if future is None:
                continue
            try:
                record.result = future.result()
            except TaskExecutorError as error:
                record.failure, record.recovery_decision = (
                    _classify_non_validation_failure(
                        task_id=record.task_id,
                        attempt_number=record.attempt_number,
                        phase=TaskExecutionFailurePhase.EXECUTOR,
                        failure_kind=TaskExecutionRecoveryFailureKind.EXECUTOR,
                        retryable=error.retryable,
                        error=error,
                        request=record.request,
                    )
                )
            except Exception:
                # Custom executors are expected to raise TaskExecutorError. An
                # unexpected exception is sanitized and terminal, but peers are
                # still joined and retained.
                error = TaskExecutorError(
                    "TaskExecutor raised an unexpected exception.",
                    retryable=False,
                )
                record.failure, record.recovery_decision = (
                    _classify_non_validation_failure(
                        task_id=record.task_id,
                        attempt_number=record.attempt_number,
                        phase=TaskExecutionFailurePhase.EXECUTOR,
                        failure_kind=TaskExecutionRecoveryFailureKind.EXECUTOR,
                        retryable=False,
                        error=error,
                        request=record.request,
                    )
                )

    # Canonicalization, validation, and recovery classification are sequential
    # in the authorized wave order, independent of physical completion timing.
    for record in records:
        if record.result is None or record.request is None:
            continue
        try:
            record.artifacts = canonicalize_execution_result(
                record.request,
                record.result,
                created_at=datetime.now(UTC).isoformat(),
            )
        except TaskExecutionContractError as error:
            record.failure, record.recovery_decision = (
                _classify_non_validation_failure(
                    task_id=record.task_id,
                    attempt_number=record.attempt_number,
                    phase=TaskExecutionFailurePhase.CANONICALIZATION,
                    failure_kind=(
                        TaskExecutionRecoveryFailureKind.CANONICALIZATION
                    ),
                    retryable=isinstance(error, TaskExecutionCorrelationError),
                    error=error,
                    request=record.request,
                )
            )
            continue

        record.validation = validate_execution_result(
            record.request, record.result, record.artifacts
        )
        if record.validation.passed:
            record.succeeded = True
        else:
            retryable, feedback = classify_validation_failure(record.validation)
            record.recovery_decision = decide_task_execution_recovery(
                task_id=record.task_id,
                attempt_number=record.attempt_number,
                request_id=record.request.request_id,
                attempt_id=record.request.attempt_id,
                failure_kind=TaskExecutionRecoveryFailureKind.VALIDATION,
                retryable=retryable,
                feedback=feedback,
            )

    settled = _settle_task_execution_wave(graph, started, records)
    terminal_decisions = [
        record.recovery_decision
        for record in records
        if record.recovery_decision is not None
        and record.recovery_decision.action is TaskExecutionRecoveryAction.FAIL_TASK
    ]
    stop_reason = " | ".join(
        _terminal_stop_reason(decision) for decision in terminal_decisions
    )
    update: WorkflowState = {
        "task_graph_execution": settled,
        "task_execution_waves": [wave],
        "task_execution_requests": [
            record.request for record in records if record.request is not None
        ],
        "task_execution_results": [
            record.result for record in records if record.result is not None
        ],
        "engineering_artifacts": [
            artifact for record in records for artifact in record.artifacts
        ],
        "task_execution_validations": [
            record.validation
            for record in records
            if record.validation is not None
        ],
        "task_execution_failures": [
            record.failure for record in records if record.failure is not None
        ],
        "task_execution_recovery_decisions": [
            record.recovery_decision
            for record in records
            if record.recovery_decision is not None
        ],
        "trace": [
            f"[execute_task_graph_step] wave {wave_number} "
            f"{record.task_id} {_wave_record_outcome(record)}"
            for record in records
        ],
    }
    if stop_reason:
        update["safe_stop_reason"] = stop_reason
    return update


def _next_task_execution_wave_number(state: WorkflowState) -> int:
    """Derive the next wave number from contiguous append-only history."""

    waves = state.get("task_execution_waves", [])
    actual = tuple(wave.wave_number for wave in waves)
    expected = tuple(range(1, len(waves) + 1))
    if actual != expected:
        raise TaskExecutionError(
            "Task execution wave history must be contiguous and ordered."
        )
    return len(waves) + 1


def _dependency_evidence(
    state: WorkflowState,
    graph: TaskGraph,
    execution: TaskGraphExecutionState,
    task_id: str,
) -> tuple[tuple[EngineeringArtifact, ...], tuple[TaskExecutionValidationResult, ...]]:
    """Select current direct-dependency evidence in canonical dependency order."""

    task = next(task for task in graph.tasks if task.task_id == task_id)
    states = {item.task_id: item for item in execution.task_states}
    requests = state.get("task_execution_requests", [])
    all_artifacts = state.get("engineering_artifacts", [])
    all_validations = state.get("task_execution_validations", [])
    artifacts_by_id = {
        artifact.artifact_id: artifact for artifact in all_artifacts
    }
    selected_artifacts: list[EngineeringArtifact] = []
    selected_validations: list[TaskExecutionValidationResult] = []
    for dependency_id in task.depends_on:
        attempt_number = states[dependency_id].attempt_count
        source_requests = [
            candidate
            for candidate in requests
            if candidate.task_id == dependency_id
            and candidate.attempt_number == attempt_number
        ]
        if len(source_requests) != 1:
            continue
        source_request = source_requests[0]
        matching_validations = [
            candidate
            for candidate in all_validations
            if candidate.task_id == dependency_id
            and candidate.request_id == source_request.request_id
            and candidate.attempt_id == source_request.attempt_id
            and candidate.passed
        ]
        selected_validations.extend(matching_validations)
        for validation in matching_validations:
            selected_artifacts.extend(
                artifacts_by_id[artifact_id]
                for artifact_id in validation.artifact_ids
                if artifact_id in artifacts_by_id
            )
    return tuple(selected_artifacts), tuple(selected_validations)


def _prior_recovery_decision(
    state: WorkflowState,
    task_id: str,
    attempt_number: int,
) -> TaskExecutionRecoveryDecision | None:
    """Select exactly the immediately prior application-owned retry decision."""

    if attempt_number == 1:
        return None
    matching = [
        decision
        for decision in state.get("task_execution_recovery_decisions", [])
        if decision.task_id == task_id
        and decision.attempt_number == attempt_number - 1
    ]
    if len(matching) != 1:
        raise TaskExecutionContractError(
            f"Task {task_id} attempt {attempt_number} requires exactly one "
            "immediately prior recovery decision."
        )
    return matching[0]


def _classify_non_validation_failure(
    *,
    task_id: str,
    attempt_number: int,
    phase: TaskExecutionFailurePhase,
    failure_kind: TaskExecutionRecoveryFailureKind,
    retryable: bool,
    error: Exception,
    request: TaskExecutionRequest | None = None,
) -> tuple[TaskExecutionFailure, TaskExecutionRecoveryDecision]:
    """Create immutable failure and recovery evidence without settlement."""

    feedback = _safe_recovery_feedback(failure_kind, error)
    decision = decide_task_execution_recovery(
        task_id=task_id,
        attempt_number=attempt_number,
        request_id=request.request_id if request is not None else None,
        attempt_id=request.attempt_id if request is not None else None,
        failure_kind=failure_kind,
        retryable=retryable,
        feedback=feedback,
    )
    failure = TaskExecutionFailure(
        task_id=task_id,
        attempt_number=attempt_number,
        request_id=request.request_id if request is not None else None,
        attempt_id=request.attempt_id if request is not None else None,
        phase=phase,
        error_type=type(error).__name__,
        message=str(error),
    )
    return failure, decision


def _safe_recovery_feedback(
    failure_kind: TaskExecutionRecoveryFailureKind,
    error: Exception,
) -> str:
    """Create concise feedback without stack traces or provider internals."""

    return f"{failure_kind.value} failed: {error}"


def _settle_task_execution_wave(
    graph: TaskGraph,
    execution: TaskGraphExecutionState,
    records: list[_WaveAttemptRecord],
) -> TaskGraphExecutionState:
    """Settle a classified quiescent wave in deterministic canonical phases."""

    for record in records:
        if not record.succeeded and record.recovery_decision is None:
            raise TaskExecutionError(
                f"Task {record.task_id} has no classified wave outcome."
            )

    settled = execution
    # Retry transitions must occur while the graph is still RUNNING, even when
    # another member will terminally fail this same wave.
    for record in records:
        decision = record.recovery_decision
        if (
            decision is not None
            and decision.action is TaskExecutionRecoveryAction.RETRY
        ):
            settled = prepare_task_retry(graph, settled, record.task_id)

    # Terminal failures freeze all future dispatch before successful peers settle.
    for record in records:
        decision = record.recovery_decision
        if (
            decision is not None
            and decision.action is TaskExecutionRecoveryAction.FAIL_TASK
        ):
            settled = mark_task_failed(graph, settled, record.task_id)

    # Peers that were already RUNNING retain their valid evidence and may settle
    # after graph failure without unlocking new work.
    for record in records:
        if record.succeeded:
            settled = mark_task_succeeded(graph, settled, record.task_id)
    return settled


def _terminal_stop_reason(decision: TaskExecutionRecoveryDecision) -> str:
    return (
        f"Task {decision.task_id} terminally failed after "
        f"{decision.failure_kind.value}: {decision.reason} "
        f"Feedback: {decision.feedback}"
    )


def _wave_record_outcome(record: _WaveAttemptRecord) -> str:
    if record.succeeded:
        return "succeeded"
    if record.recovery_decision is None:
        raise TaskExecutionError(
            f"Task {record.task_id} has no classified wave outcome."
        )
    if record.recovery_decision.action is TaskExecutionRecoveryAction.RETRY:
        return (
            "scheduled retry after "
            f"{record.recovery_decision.failure_kind.value.lower()}"
        )
    return "terminally failed"


def safe_stop(state: WorkflowState) -> WorkflowState:
    """Terminate governed planning or a failed quiescent graph execution."""

    execution_value = state.get("task_graph_execution")
    stopped_execution: TaskGraphExecutionState | None = None
    if execution_value is not None and _execution_from_state(
        execution_value
    ).status is TaskGraphExecutionStatus.FAILED:
        stopped_execution = safe_stop_task_graph_execution(
            _task_graph_from_data(state["approved_task_graph"]),
            _execution_from_state(execution_value),
        )
        reason = state.get("safe_stop_reason") or (
            "TaskGraph execution failed and was stopped safely."
        )
    elif state.get("requirement_analysis_status") == "failed":
        reason = state.get("requirement_analysis_error") or (
            REQUIREMENT_ANALYSIS_ATTEMPTS_REASON
        )
        if state.get("requirement_analysis_retryable"):
            reason = f"{REQUIREMENT_ANALYSIS_ATTEMPTS_REASON} Last error: {reason}"
    elif state.get("requirement_review_decision") == "REJECT":
        reason = REQUIREMENT_ANALYSIS_REJECTED_REASON
    elif state.get("requirement_review_decision") == "REQUEST_CHANGES":
        reason = MAX_REQUIREMENT_REVISIONS_REASON
    elif state.get("task_planning_status") == "failed":
        reason = state.get("task_planning_error") or TASK_PLANNING_ATTEMPTS_REASON
        if state.get("task_planning_retryable"):
            reason = f"{TASK_PLANNING_ATTEMPTS_REASON} Last error: {reason}"
    elif state.get("task_graph_decision") == "REJECT":
        reason = TASK_GRAPH_REJECTED_REASON
    else:
        reason = MAX_TASK_GRAPH_REVISIONS_REASON
    update: WorkflowState = {
        "safe_stop_reason": reason,
        "exit_gate_passed": False,
        "workflow_status": "safe_stopped",
        "errors": [*state.get("errors", []), reason],
        "trace": ["[safe_stop] complete"],
    }
    if stopped_execution is not None:
        update["task_graph_execution"] = stopped_execution
    return update


def exit_gate(state: WorkflowState) -> WorkflowState:
    """Validate governed planning and deterministic TaskGraph execution outputs."""

    execution = state.get("task_graph_execution")

    validations = {
        "processed requirements": bool(
            state.get("entry_gate_passed") and state.get("normalized_requirements")
        ),
        "approved requirement analysis": bool(
            state.get("requirement_analysis_status") == "validated"
            and state.get("requirement_analysis")
            and state.get("requirement_review_decision") == "APPROVE"
        ),
        "approved requirement specification": bool(
            state.get("approved_requirement_spec")
        ),
        "validated task graph": bool(
            state.get("task_planning_status") == "validated"
            and state.get("candidate_task_graph")
            and state.get("task_graph_semantics")
        ),
        "approved task graph": bool(
            state.get("task_graph_decision") == "APPROVE"
            and state.get("approved_task_graph")
            and state.get("task_graph_review_history")
        ),
        "successful TaskGraph execution": bool(
            execution is not None
            and _execution_from_state(execution).status
            is TaskGraphExecutionStatus.SUCCEEDED
        ),
        "complete execution evidence": bool(
            _has_complete_final_execution_evidence(state)
        ),
    }
    missing = [label for label, passed in validations.items() if not passed]
    if missing:
        reason = "Exit gate failed; incomplete output: " + ", ".join(missing)
        return {
            "exit_gate_passed": False,
            "workflow_status": "exit_gate_failed",
            "errors": [*state.get("errors", []), reason],
            "trace": ["[exit_gate] failed"],
        }
    return {
        "exit_gate_passed": True,
        "workflow_status": "success",
        "trace": ["[exit_gate] passed"],
    }


def _has_complete_final_execution_evidence(state: WorkflowState) -> bool:
    """Require one exact successful evidence chain for every final task attempt."""

    graph_data = state.get("approved_task_graph")
    execution_value = state.get("task_graph_execution")
    if not graph_data or execution_value is None:
        return False
    graph = _task_graph_from_data(graph_data)
    execution = _execution_from_state(execution_value)
    runtime_states = {item.task_id: item for item in execution.task_states}
    requests = state.get("task_execution_requests", [])
    results = state.get("task_execution_results", [])
    validations = state.get("task_execution_validations", [])
    artifacts = state.get("engineering_artifacts", [])

    for task in graph.tasks:
        runtime = runtime_states.get(task.task_id)
        if (
            runtime is None
            or runtime.status is not TaskExecutionStatus.SUCCEEDED
            or runtime.attempt_count < 1
        ):
            return False
        final_requests = [
            request
            for request in requests
            if request.task_id == task.task_id
            and request.attempt_number == runtime.attempt_count
        ]
        if len(final_requests) != 1:
            return False
        request = final_requests[0]
        final_results = [
            result
            for result in results
            if result.task_id == task.task_id
            and result.request_id == request.request_id
            and result.attempt_id == request.attempt_id
        ]
        if len(final_results) != 1:
            return False
        final_validations = [
            validation
            for validation in validations
            if validation.task_id == task.task_id
            and validation.request_id == request.request_id
            and validation.attempt_id == request.attempt_id
            and validation.passed
        ]
        if len(final_validations) != 1:
            return False
        final_artifacts = sorted(
            (
                artifact
                for artifact in artifacts
                if artifact.task_id == task.task_id
                and artifact.request_id == request.request_id
                and artifact.attempt_id == request.attempt_id
                and artifact.attempt_number == runtime.attempt_count
            ),
            key=lambda artifact: (artifact.output_index, artifact.artifact_id),
        )
        if tuple(artifact.artifact_id for artifact in final_artifacts) != (
            final_validations[0].artifact_ids
        ):
            return False
    return True


def _validated_approval_response(
    response: ApprovalResponse, *, checkpoint: str
) -> tuple[ApprovalDecision, str]:
    decision = response.get("decision")
    if decision not in {"APPROVE", "REQUEST_CHANGES", "REJECT"}:
        raise ValueError(f"Unsupported {checkpoint} approval decision.")
    feedback = response.get("feedback", "").strip()
    if decision == "REQUEST_CHANGES" and not feedback:
        raise ValueError("REQUEST_CHANGES requires human feedback.")
    return cast(ApprovalDecision, decision), feedback


def _requirement_analysis_failure(
    state: WorkflowState,
    *,
    attempt_number: int,
    reason: str,
    retryable: bool,
) -> RequirementAnalysisFailure:
    return {
        "sequence": len(state.get("requirement_analysis_failures", [])) + 1,
        "revision_number": state.get("requirement_analysis_revision_count", 0),
        "attempt_number": attempt_number,
        "reason": reason,
        "retryable": retryable,
    }


def _task_planning_failure(
    state: WorkflowState,
    *,
    attempt_number: int,
    reason: str,
    retryable: bool,
) -> TaskPlanningFailure:
    return {
        "sequence": len(state.get("task_planning_failures", [])) + 1,
        "revision_number": state.get("task_graph_revision_count", 0),
        "attempt_number": attempt_number,
        "reason": reason,
        "retryable": retryable,
    }


def _failed_task_graph_validation(
    state: WorkflowState, reason: str
) -> WorkflowState:
    failure = _task_planning_failure(
        state,
        attempt_number=state["task_planning_attempt_count"],
        reason=reason,
        retryable=True,
    )
    return {
        "task_planning_candidate": None,
        "task_planning_status": "failed",
        "task_planning_retryable": True,
        "task_planning_error": reason,
        "task_planning_failures": [failure],
        "trace": ["[normalize_and_validate_task_graph] failed"],
    }


def _pydantic_failure_reason(prefix: str, error: ValidationError) -> str:
    first_error = error.errors(include_url=False)[0]
    location = ".".join(str(part) for part in first_error["loc"]) or "root"
    return f"{prefix} failed at {location}: {first_error['msg']}."


def _spec_from_state(state: WorkflowState) -> ApprovedRequirementSpec:
    return ApprovedRequirementSpec.model_validate_json(
        json.dumps(state["approved_requirement_spec"])
    )


def _task_graph_from_data(data: TaskGraphData) -> TaskGraph:
    return TaskGraph.model_validate_json(json.dumps(data))


def _execution_from_state(data: object) -> TaskGraphExecutionState:
    if isinstance(data, TaskGraphExecutionState):
        return data
    return TaskGraphExecutionState.model_validate(data)
