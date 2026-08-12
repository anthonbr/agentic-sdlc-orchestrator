"""Command-line entry point for governed requirement submissions."""

from __future__ import annotations

import argparse
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from agentic_sdlc.application import GovernedRunRequest, GovernedRunService
from agentic_sdlc.project_export import (
    ProjectNameError,
    normalize_project_name,
)
from agentic_sdlc.run_artifacts import (
    SDLC_ARTIFACT_DIRECTORY_NAME,
)
from agentic_sdlc.requirement_submission import (
    RequirementSubmissionError,
    deterministic_project_name,
    resolve_inline_requirement,
    resolve_requirement_file,
)
from agentic_sdlc.state import (
    ApprovalResponse,
    WorkflowState,
    demo_input,
    workflow_input_from_submission,
)
from agentic_sdlc.task_execution_progress import (
    ConsoleTaskExecutionProgressReporter,
)


def main(arguments: list[str] | None = None) -> int:
    """Resolve one requirement source and run the shared governed workflow."""

    args = list(sys.argv[1:] if arguments is None else arguments)
    parser = _argument_parser()
    try:
        parsed = parser.parse_args(args)
    except SystemExit as error:
        return int(error.code)

    requested_project_name = parsed.project_name
    try:
        normalized_project_name = (
            normalize_project_name(requested_project_name)
            if requested_project_name is not None
            else None
        )
    except ProjectNameError as error:
        _print_cli_error(parser, f"Invalid project name: {error}")
        return 2

    try:
        if parsed.command == "demo":
            workflow_input = demo_input()
        else:
            submission = (
                resolve_inline_requirement(parsed.requirement)
                if parsed.requirement is not None
                else resolve_requirement_file(parsed.requirement_file)
            )
            workflow_input = workflow_input_from_submission(
                submission,
                project_name=(
                    normalized_project_name
                    if normalized_project_name is not None
                    else deterministic_project_name(submission)
                ),
            )
    except RequirementSubmissionError as error:
        _print_cli_error(parser, str(error))
        return 2

    return _execute_workflow(
        workflow_input,
        command=parsed.command,
        requested_project_name=requested_project_name,
    )


def _execute_workflow(
    workflow_input: WorkflowState,
    *,
    command: str,
    requested_project_name: str | None,
) -> int:
    """Present one governed run coordinated by the shared application service."""

    service = GovernedRunService(repository_root=Path.cwd())
    snapshot = service.start_run(
        GovernedRunRequest(
            command=command,
            workflow_input=workflow_input,
            requested_project_name=requested_project_name,
        ),
        progress_reporter=ConsoleTaskExecutionProgressReporter(),
    )
    if snapshot.workflow_diagram_generated:
        print(
            "Workflow diagram written to: "
            f"{snapshot.artifact_bundle.workflow_diagram_path}"
        )
    for warning in snapshot.warnings:
        print(f"Warning: {warning}", file=sys.stderr)

    while snapshot.human_gate is not None:
        payload = snapshot.human_gate.payload
        if snapshot.human_gate.stage == "requirement_analysis_review":
            response = _prompt_for_requirement_analysis_decision(payload)
        elif snapshot.human_gate.stage == "task_graph_review":
            response = _prompt_for_task_graph_decision(payload)
        else:
            raise ValueError("The workflow paused at an unknown review stage.")
        snapshot = service.resume_run(
            snapshot.run_id,
            response,
            gate_token=snapshot.human_gate.gate_token,
        )

    state = snapshot.workflow_state
    artifact_dir = snapshot.artifact_bundle.artifact_dir
    for event in state.get("trace", []):
        print(event)

    if state.get("workflow_status") == "success":
        print("Workflow completed successfully.")
        session = state["governed_workspace_session"]
        print(f"Workspace integrity: {session.integrity_status.value}")
        if snapshot.application_error is not None:
            print(snapshot.application_error, file=sys.stderr)
            print("Run evidence written to:")
            print(f"  {artifact_dir}")
            return 1
        export_result = snapshot.export_result
        if export_result is None or not export_result.succeeded:
            print("Project export failed: no verified result.", file=sys.stderr)
            print("Run evidence written to:")
            print(f"  {artifact_dir}")
            return 1
        print("Project exported successfully.")
        print(f"Project: {export_result.project_name}")
        print("Durable project exported to:")
        print(f"  {export_result.destination_directory}")
        print("Run evidence written to:")
        print(f"  {artifact_dir}")
        print("Packaged SDLC evidence:")
        print(
            f"  {export_result.destination_directory / SDLC_ARTIFACT_DIRECTORY_NAME}"
        )
        return 0

    if state.get("workflow_status") == "safe_stopped":
        print(f"Workflow stopped safely: {state['safe_stop_reason']}")
        print("Partial run evidence written to:")
        print(f"  {artifact_dir}")
        return 1

    print(f"Workflow failed: {state.get('workflow_status', 'unknown')}")
    for error in state.get("errors", []):
        print(f"- {error}")
    return 1


def _argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m agentic_sdlc")
    commands = parser.add_subparsers(dest="command", required=True)

    demo_parser = commands.add_parser(
        "demo", help="Run the built-in URL Shortener demonstration."
    )
    demo_parser.add_argument("--project-name", metavar="NAME")

    run_parser = commands.add_parser(
        "run", help="Run one user-supplied natural-language requirement."
    )
    requirement_source = run_parser.add_mutually_exclusive_group(required=True)
    requirement_source.add_argument(
        "--requirement",
        metavar="TEXT",
        help="Inline natural-language software requirement.",
    )
    requirement_source.add_argument(
        "--requirement-file",
        type=Path,
        metavar="PATH",
        help="UTF-8 file containing a natural-language software requirement.",
    )
    run_parser.add_argument("--project-name", metavar="NAME")
    return parser


def _print_cli_error(parser: argparse.ArgumentParser, message: str) -> None:
    parser.print_usage(sys.stderr)
    print(f"{parser.prog}: error: {message}", file=sys.stderr)


def _prompt_for_requirement_analysis_decision(
    payload: Mapping[str, Any],
) -> ApprovalResponse:
    """Show the complete analysis boundary before collecting human authority."""

    analysis = payload["requirement_analysis"]
    print("\nRequirement analysis requires human review.")
    print(f"Current revision: {payload['revision_number']}")
    print(f"Normalized problem: {analysis['normalized_problem_statement']}")
    print(f"Requirement type: {analysis['requirement_type']}")
    _print_analysis_list("Functional requirements", analysis["functional_requirements"])
    _print_analysis_list(
        "Nonfunctional requirements", analysis["nonfunctional_requirements"]
    )
    _print_analysis_list("Constraints", analysis["constraints"])
    _print_analysis_list("Ambiguities", analysis["ambiguities"])
    _print_analysis_list("Assumptions", analysis["assumptions"])
    _print_analysis_list("Acceptance criteria", analysis["acceptance_criteria"])
    _print_analysis_list("Risks", analysis["risks"])
    print(f"Needs clarification: {analysis['needs_clarification']}")
    readiness = payload["planning_readiness"]
    print(f"Planning readiness: {readiness['status']}")
    print(f"Readiness reason: {readiness['reason_code'] or 'None'}")
    print(f"Confidence: {analysis['confidence']:.2f}")
    return _prompt_for_decision(payload["allowed_decisions"])


def _print_analysis_list(label: str, values: Sequence[str]) -> None:
    print(f"{label}:")
    if not values:
        print("  - None identified.")
        return
    for value in values:
        print(f"  - {value}")


def _prompt_for_task_graph_decision(
    payload: Mapping[str, Any],
) -> ApprovalResponse:
    """Show canonical identities and derived layers before human authority."""

    spec = payload["approved_requirement_spec"]
    delivery_policy = payload["project_delivery_policy"]
    graph = payload["candidate_task_graph"]
    semantics = payload["graph_semantics"]
    tasks = {task["task_id"]: task for task in graph["tasks"]}

    print("\nEngineering task graph requires human review.")
    print(
        f"Approved requirement spec: {spec['spec_id']} "
        f"(analysis revision {spec['source_analysis_revision']})"
    )
    print(f"Project delivery policy: {delivery_policy['mode']}")
    for label, field_name in (
        ("Functional", "functional_requirements"),
        ("Nonfunctional", "nonfunctional_requirements"),
        ("Constraints", "constraints"),
        ("Acceptance criteria", "acceptance_criteria"),
        ("Risks", "risks"),
        ("Ambiguities", "ambiguities"),
    ):
        values = spec[field_name]
        if values:
            print(f"{label} IDs: " + ", ".join(item["item_id"] for item in values))

    print(f"TaskGraph revision: {payload['revision_number']}")
    for layer_number, task_ids in enumerate(
        semantics["execution_layers"], start=1
    ):
        suffix = " — parallel" if len(task_ids) > 1 else ""
        print(f"\nLayer {layer_number}{suffix}")
        for task_id in task_ids:
            task = tasks[task_id]
            print(f"  {task_id}  {task['title']}")
            print(f"    Type: {task['task_type']}")
            print(
                "    Materialization policy: "
                f"{task['materialization_policy']}"
            )
            print(
                "    Delivery roles: "
                + (", ".join(task["deliverable_roles"]) or "None")
            )
            print(
                "    Depends on: "
                + (", ".join(task["depends_on"]) or "ENTRY")
            )
            print(
                "    Requirements: "
                + (", ".join(task["requirement_refs"]) or "None")
            )
            print(
                "    Acceptance: "
                + (", ".join(task["acceptance_criteria_refs"]) or "None")
            )
            print("    Risks: " + (", ".join(task["risk_refs"]) or "None"))
            print(
                "    Ambiguities: "
                + (", ".join(task["ambiguity_refs"]) or "None")
            )
    print(
        "\nSynchronization points: "
        + (", ".join(semantics["synchronization_points"]) or "None")
    )
    print(
        "EXIT predecessors: " + ", ".join(semantics["exit_predecessor_tasks"])
    )
    return _prompt_for_decision(payload["allowed_decisions"])


def _prompt_for_decision(
    allowed_decisions: Sequence[str] | None = None,
) -> ApprovalResponse:
    all_choices = {"a": "APPROVE", "c": "REQUEST_CHANGES", "r": "REJECT"}
    allowed = set(
        all_choices.values() if allowed_decisions is None else allowed_decisions
    )
    choices = {
        key: decision for key, decision in all_choices.items() if decision in allowed
    }
    if not choices:
        raise ValueError("Human review exposes no supported decisions.")
    labels = {
        "a": "[A] Approve",
        "c": "[C] Request changes",
        "r": "[R] Reject",
    }
    prompt = "  ".join(labels[key] for key in all_choices if key in choices) + ": "
    expected = ", ".join(key.upper() for key in choices)
    while True:
        choice = input(prompt).strip().lower()
        if choice in choices:
            break
        if len(choices) == 3:
            print("Please enter A, C, or R.")
        else:
            print(f"Please enter {expected.replace(', ', ' or ')}.")

    feedback = ""
    if choice == "c":
        feedback = _prompt_for_revision_feedback()

    return {"decision": choices[choice], "feedback": feedback}


def _prompt_for_revision_feedback() -> str:
    """Collect one or more feedback lines terminated by a blank line."""

    while True:
        print("Requested changes (finish with a blank line):")
        feedback_lines: list[str] = []
        while True:
            line = input()
            if not line.strip():
                break
            feedback_lines.append(line.strip())

        if feedback_lines:
            return "\n".join(feedback_lines)
        print("Feedback is required when requesting changes.")


if __name__ == "__main__":
    raise SystemExit(main())
