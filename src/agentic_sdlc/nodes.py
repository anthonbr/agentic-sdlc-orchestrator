"""Deterministic workflow nodes for the V0.1 orchestration prototype."""

from __future__ import annotations

from agentic_sdlc.state import (
    ArchitectureArtifact,
    PlanStep,
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

    return {
        "project_name": state.get("project_name", "").strip(),
        "requirements": original_requirements,
        "normalized_requirements": normalized_requirements,
        "entry_gate_passed": False,
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
        "trace": ["[create_implementation_plan] complete"],
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
    """Validate all required V0.1 outputs before declaring success."""

    validations = {
        "processed requirements": bool(
            state.get("entry_gate_passed")
            and state.get("normalized_requirements")
        ),
        "requirement decomposition": bool(state.get("work_items")),
        "implementation plan": bool(state.get("implementation_plan")),
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
