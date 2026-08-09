"""Governed workflow nodes for the V0.3 orchestration prototype."""

from __future__ import annotations

from typing import cast

from langgraph.types import interrupt
from pydantic import ValidationError

from agentic_sdlc.llm import RequirementAnalysisClient, RequirementAnalysisClientError
from agentic_sdlc.prompts import REQUIREMENT_ANALYSIS_PROMPT_VERSION
from agentic_sdlc.requirement_analysis import RequirementAnalysis
from agentic_sdlc.state import (
    MAX_PLAN_REVISIONS_REASON,
    MAX_REQUIREMENT_REVISIONS_REASON,
    PLAN_REJECTED_REASON,
    REQUIREMENT_ANALYSIS_ATTEMPTS_REASON,
    REQUIREMENT_ANALYSIS_REJECTED_REASON,
    ApprovalDecision,
    ApprovalEvent,
    ApprovalResponse,
    ArchitectureArtifact,
    PlanStep,
    RequirementAnalysisData,
    RequirementAnalysisFailure,
    RequirementAnalysisRecord,
    TestPlanArtifact,
    WorkItem,
    WorkflowState,
)


def requirements_intake(state: WorkflowState) -> WorkflowState:
    """Preserve the submitted requirements and create a normalized form."""

    original_requirements = list(state.get("requirements", []))
    normalized_requirement_texts = [
        requirement.strip()
        for requirement in original_requirements
        if requirement.strip()
    ]
    normalized_requirements = [
        {"id": f"REQ-{index:03d}", "text": requirement_text}
        for index, requirement_text in enumerate(
            normalized_requirement_texts, start=1
        )
    ]
    raw_requirement = state.get("raw_requirement", "").strip()
    if not raw_requirement:
        raw_requirement = "\n".join(normalized_requirement_texts)

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
        "implementation_plan_decision": None,
        "approval_feedback": "",
        "plan_revision_count": 0,
        "approval_history": [],
        "safe_stop_reason": "",
        "synchronization_complete": False,
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

    return {
        "entry_gate_passed": True,
        "trace": ["[entry_gate] passed"],
    }


def requirement_analysis_task(
    state: WorkflowState,
    *,
    client: RequirementAnalysisClient,
) -> WorkflowState:
    """Ask the injected LLM client for one structured analysis candidate."""

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
        failure = _analysis_failure(
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
        reason = _validation_failure_reason(error)
        failure = _analysis_failure(
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

    analysis_data = cast(
        RequirementAnalysisData, analysis.model_dump(mode="json")
    )
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
    """Prepare the next machine retry without changing human revision lineage."""

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


def _analysis_failure(
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


def _validation_failure_reason(error: ValidationError) -> str:
    first_error = error.errors(include_url=False)[0]
    location = ".".join(str(part) for part in first_error["loc"]) or "root"
    return (
        "Structured requirement analysis validation failed at "
        f"{location}: {first_error['msg']}."
    )


def decompose_requirements(state: WorkflowState) -> WorkflowState:
    """Turn normalized requirements into traceable engineering work items."""

    work_items: list[WorkItem] = []
    for index, requirement in enumerate(state["normalized_requirements"], start=1):
        work_items.append(
            {
                "id": f"WI-{index:03d}",
                "source_requirement_id": requirement["id"],
                "source_requirement": requirement["text"],
                "action": _work_item_action(requirement["text"]),
            }
        )

    return {
        "work_items": work_items,
        "trace": ["[decompose_requirements] complete"],
    }


def _work_item_action(requirement: str) -> str:
    known_actions = {
        "accept a long url.": "Define URL submission handling and validate the long URL.",
        "generate a unique short url.": (
            "Define short-code generation with a uniqueness guarantee."
        ),
        "redirect the short url to the original url.": (
            "Define short-code lookup and redirect behavior."
        ),
        "return an error for unknown short urls.": (
            "Define the error response for an unknown short code."
        ),
    }
    return known_actions.get(
        requirement.casefold(),
        f"Implement and verify this requirement: {requirement}",
    )


def create_implementation_plan(state: WorkflowState) -> WorkflowState:
    """Create an ordered engineering plan without implementing the application."""

    all_work_item_ids = [item["id"] for item in state["work_items"]]
    available_work_item_ids = set(all_work_item_ids)
    plan_definitions = (
        (
            "Define service and API boundaries for submission and redirect requests.",
            ["WI-001", "WI-003", "WI-004"],
        ),
        (
            "Define short-code generation behavior and uniqueness constraints.",
            ["WI-002"],
        ),
        (
            "Define a persistence interface for short-code-to-URL mappings.",
            ["WI-002", "WI-003"],
        ),
        ("Plan redirect resolution for known short codes.", ["WI-003"]),
        (
            "Define validation and error handling for malformed input and unknown codes.",
            ["WI-001", "WI-004"],
        ),
        ("Create automated tests for the requirement set.", all_work_item_ids),
        ("Prepare API and operating documentation.", all_work_item_ids),
    )
    implementation_plan: list[PlanStep] = [
        {
            "order": index,
            "action": action,
            "work_item_ids": [
                work_item_id
                for work_item_id in related_work_item_ids
                if work_item_id in available_work_item_ids
            ],
        }
        for index, (action, related_work_item_ids) in enumerate(
            plan_definitions, start=1
        )
    ]

    return {
        "implementation_plan": implementation_plan,
        "workflow_status": "awaiting_approval",
        "trace": ["[create_implementation_plan] complete"],
    }


def implementation_plan_approval(state: WorkflowState) -> WorkflowState:
    """Pause for a human decision and record the response on resume."""

    response = cast(
        ApprovalResponse,
        interrupt(
            {
                "stage": "implementation_plan_review",
                "checkpoint": "implementation_plan",
                "message": "Implementation plan requires approval.",
                "implementation_plan": state["implementation_plan"],
                "revision_number": state.get("plan_revision_count", 0),
                "allowed_decisions": ["APPROVE", "REQUEST_CHANGES", "REJECT"],
            }
        ),
    )
    decision, feedback = _validated_approval_response(
        response, checkpoint="implementation-plan"
    )

    revision_number = state.get("plan_revision_count", 0)
    event: ApprovalEvent = {
        "sequence": len(state.get("approval_history", [])) + 1,
        "checkpoint": "implementation_plan",
        "decision": decision,
        "feedback": feedback,
        "revision_number": revision_number,
    }
    return {
        "implementation_plan_decision": decision,
        "approval_feedback": feedback,
        "approval_history": [event],
        "workflow_status": "pending",
        "trace": [f"[implementation_plan_approval] {decision.lower()}"],
    }


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


def revise_implementation_plan(state: WorkflowState) -> WorkflowState:
    """Append a deterministic plan action that preserves the human feedback."""

    revision_number = state.get("plan_revision_count", 0) + 1
    revised_plan = [*state["implementation_plan"]]
    revised_plan.append(
        {
            "order": len(revised_plan) + 1,
            "action": (
                f"Address implementation-plan review feedback (revision "
                f"{revision_number}): {state['approval_feedback']}"
            ),
            "work_item_ids": [item["id"] for item in state["work_items"]],
        }
    )
    return {
        "implementation_plan": revised_plan,
        "implementation_plan_decision": None,
        "approval_feedback": "",
        "plan_revision_count": revision_number,
        "workflow_status": "awaiting_approval",
        "trace": [f"[revise_implementation_plan] revision {revision_number} complete"],
    }


def safe_stop(state: WorkflowState) -> WorkflowState:
    """Terminate governed execution without running downstream design work."""

    if state.get("requirement_analysis_status") == "failed":
        reason = state.get("requirement_analysis_error") or (
            REQUIREMENT_ANALYSIS_ATTEMPTS_REASON
        )
        if state.get("requirement_analysis_retryable"):
            reason = f"{REQUIREMENT_ANALYSIS_ATTEMPTS_REASON} Last error: {reason}"
    elif state.get("requirement_review_decision") == "REJECT":
        reason = REQUIREMENT_ANALYSIS_REJECTED_REASON
    elif state.get("requirement_review_decision") == "REQUEST_CHANGES":
        reason = MAX_REQUIREMENT_REVISIONS_REASON
    elif state.get("implementation_plan_decision") == "REJECT":
        reason = PLAN_REJECTED_REASON
    else:
        reason = MAX_PLAN_REVISIONS_REASON
    return {
        "safe_stop_reason": reason,
        "synchronization_complete": False,
        "exit_gate_passed": False,
        "workflow_status": "safe_stopped",
        "errors": [*state.get("errors", []), reason],
        "trace": ["[safe_stop] complete"],
    }


def architecture_task(state: WorkflowState) -> WorkflowState:
    """Produce a concise conceptual design from the plan and requirements."""

    requirement_count = len(state["normalized_requirements"])
    plan_step_count = len(state["implementation_plan"])
    architecture: ArchitectureArtifact = {
        "summary": (
            "A small service design covering "
            f"{requirement_count} requirements through {plan_step_count} planned steps."
        ),
        "components": [
            "API layer — accepts long URLs and exposes short-link redirects.",
            "URL shortening service — creates unique short codes.",
            "Persistence abstraction — maps short codes to original URLs.",
            "Redirect handler — resolves known codes and reports unknown ones.",
        ],
        "design_notes": [
            "Keep transport, shortening logic, and storage concerns separate.",
            "Define the storage boundary now; choose a concrete database later.",
            "Treat unknown-code behavior as an explicit API contract.",
        ],
    }
    return {
        "architecture": architecture,
        "trace": ["[architecture_task] complete"],
    }


def test_plan_task(state: WorkflowState) -> WorkflowState:
    """Produce a test strategy independently of the architecture branch."""

    test_plan: TestPlanArtifact = {
        "strategy": (
            "Verify each requirement at the service boundary, then cover validation "
            "and repeat-request behavior."
        ),
        "cases": [
            {
                "name": "Valid URL shortening",
                "purpose": "A valid long URL produces a usable short URL.",
            },
            {
                "name": "Unique short-code creation",
                "purpose": "Distinct stored URLs do not receive colliding codes.",
            },
            {
                "name": "Redirect correctness",
                "purpose": "A known short code redirects to its original URL.",
            },
            {
                "name": "Unknown short code",
                "purpose": "An unknown code returns the defined error response.",
            },
            {
                "name": "Malformed input",
                "purpose": "An invalid long URL is rejected clearly.",
            },
            {
                "name": "Repeated request",
                "purpose": (
                    "Define and verify whether repeated submissions reuse an existing "
                    "short code or generate a new one."
                ),
            },
        ],
    }
    return {
        "test_plan": test_plan,
        "trace": ["[test_plan_task] complete"],
    }


def synchronize(state: WorkflowState) -> WorkflowState:
    """Confirm that both parallel branch artifacts reached the join."""

    missing_outputs: list[str] = []
    architecture = state.get("architecture")
    if not architecture or not architecture.get("components"):
        missing_outputs.append("architecture")
    test_plan = state.get("test_plan")
    if not test_plan or not test_plan.get("cases"):
        missing_outputs.append("test plan")

    if missing_outputs:
        reason = "Synchronization failed; missing output: " + ", ".join(
            missing_outputs
        )
        return {
            "synchronization_complete": False,
            "workflow_status": "synchronization_failed",
            "errors": [*state.get("errors", []), reason],
            "trace": ["[synchronize] failed"],
        }

    return {
        "synchronization_complete": True,
        "trace": ["[synchronize] complete"],
    }


def exit_gate(state: WorkflowState) -> WorkflowState:
    """Validate all required V0.3 outputs before declaring success."""

    validations = {
        "processed requirements": bool(
            state.get("entry_gate_passed")
            and state.get("normalized_requirements")
        ),
        "approved requirement analysis": bool(
            state.get("requirement_analysis_status") == "validated"
            and state.get("requirement_analysis")
            and state.get("requirement_review_decision") == "APPROVE"
            and state.get("requirement_review_history")
        ),
        "requirement decomposition": bool(state.get("work_items")),
        "implementation plan": bool(state.get("implementation_plan")),
        "implementation plan approval": bool(
            state.get("implementation_plan_decision") == "APPROVE"
            and state.get("approval_history")
        ),
        "architecture": bool(
            state.get("architecture")
            and state["architecture"].get("components")
        ),
        "test plan": bool(
            state.get("test_plan") and state["test_plan"].get("cases")
        ),
        "successful synchronization": bool(state.get("synchronization_complete")),
    }
    missing_outputs = [name for name, is_valid in validations.items() if not is_valid]

    if missing_outputs:
        reason = "Exit gate failed; incomplete output: " + ", ".join(
            missing_outputs
        )
        status = (
            "synchronization_failed"
            if state.get("workflow_status") == "synchronization_failed"
            else "exit_gate_failed"
        )
        return {
            "exit_gate_passed": False,
            "workflow_status": status,
            "errors": [*state.get("errors", []), reason],
            "trace": ["[exit_gate] failed"],
        }

    return {
        "exit_gate_passed": True,
        "workflow_status": "success",
        "trace": ["[exit_gate] passed"],
    }
