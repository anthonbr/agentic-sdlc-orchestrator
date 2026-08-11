"""Focused V0.11 tests for the pre-workflow requirement input boundary."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import agentic_sdlc.__main__ as cli
import pytest

from agentic_sdlc.artifacts import _requirement_analysis_markdown
from agentic_sdlc.llm import FakeRequirementAnalysisClient, FakeTaskPlanningClient
from agentic_sdlc.nodes import requirements_intake
from agentic_sdlc.project_delivery import ProjectDeliveryMode
from agentic_sdlc.project_export import normalize_project_name
from agentic_sdlc.requirement_analysis import RequirementAnalysis
from agentic_sdlc.requirement_submission import (
    RequirementSourceKind,
    RequirementSubmissionError,
    deterministic_project_name,
    normalize_requirement_text,
    resolve_inline_requirement,
    resolve_requirement_file,
)
from agentic_sdlc.state import (
    DEMO_RAW_REQUIREMENT,
    DEMO_REQUIREMENTS,
    WorkflowState,
    demo_input,
    workflow_input_from_submission,
)
from agentic_sdlc.workflow import build_workflow, resume_workflow, run_workflow


def _analysis(version: str = "v1") -> RequirementAnalysis:
    return RequirementAnalysis(
        normalized_problem_statement=f"{version}: Build the submitted software.",
        requirement_type="greenfield",
        functional_requirements=["Implement the submitted user story."],
        nonfunctional_requirements=[],
        constraints=[],
        ambiguities=[],
        assumptions=[],
        acceptance_criteria=["The submitted user story is satisfied."],
        risks=[],
        needs_clarification=False,
        confidence=0.9,
    )


def _capture_cli_execution(
    monkeypatch: pytest.MonkeyPatch,
) -> list[tuple[WorkflowState, str, str | None]]:
    calls: list[tuple[WorkflowState, str, str | None]] = []

    def capture(
        workflow_input: WorkflowState,
        *,
        command: str,
        requested_project_name: str | None,
    ) -> int:
        calls.append((workflow_input, command, requested_project_name))
        return 0

    monkeypatch.setattr(cli, "_execute_workflow", capture)
    return calls


def _requirement_markdown_state(raw_requirement: str) -> WorkflowState:
    return {
        "raw_requirement": raw_requirement,
        "requirement_analysis": _analysis().model_dump(mode="json"),
        "requirement_analysis_history": [],
        "requirement_review_history": [],
    }


def _quoted_requirement(text: str) -> str:
    return "\n".join(f"> {line}" for line in text.splitlines())


def test_demo_input_remains_compatible_and_uses_submission_boundary() -> None:
    workflow_input = demo_input()

    assert workflow_input["project_name"] == "URL Shortener"
    assert workflow_input["requirements"] == list(DEMO_REQUIREMENTS)
    assert workflow_input["raw_requirement"] == DEMO_RAW_REQUIREMENT
    assert workflow_input["project_delivery_policy"] == {
        "mode": ProjectDeliveryMode.RUNNABLE_PROJECT.value
    }
    submission = workflow_input["requirement_submission"]
    assert submission["source_kind"] == "demo"
    assert submission["original_text"] == DEMO_RAW_REQUIREMENT
    assert submission["normalized_text"] == DEMO_RAW_REQUIREMENT


def test_inline_normalization_retains_exact_text_and_correct_hashes() -> None:
    original = (
        "\ufeff  # Inventory service\r\n\r\n"
        "1. Add an item.\r2. Preserve **Markdown**.  \r\n  "
    )
    expected = "# Inventory service\n\n1. Add an item.\n2. Preserve **Markdown**."

    submission = resolve_inline_requirement(original)

    assert normalize_requirement_text(original) == expected
    assert submission.source_kind is RequirementSourceKind.INLINE
    assert submission.original_text == original
    assert submission.normalized_text == expected
    assert submission.original_sha256 == hashlib.sha256(original.encode()).hexdigest()
    assert submission.normalized_sha256 == hashlib.sha256(expected.encode()).hexdigest()
    assert submission.source_filename is None


def test_multiline_utf8_file_is_resolved_once_with_safe_filename(
    tmp_path: Path,
) -> None:
    source = tmp_path / "requirement.md"
    original = "\ufeff\r\n  # Story\r\n\r\n- Keep punctuation!\r\n  "
    source.write_bytes(original.encode("utf-8"))

    submission = resolve_requirement_file(source)
    source.write_text("Changed after resolution.", encoding="utf-8")

    assert submission.source_kind is RequirementSourceKind.FILE
    assert submission.source_filename == "requirement.md"
    assert submission.original_text == original
    assert submission.normalized_text == "# Story\n\n- Keep punctuation!"
    assert submission.original_sha256 == hashlib.sha256(original.encode()).hexdigest()
    assert submission.normalized_sha256 == hashlib.sha256(
        submission.normalized_text.encode()
    ).hexdigest()


@pytest.mark.parametrize("text", ["", " \t\r\n ", "\ufeff  \r\n"])
def test_empty_normalized_submission_is_rejected(text: str) -> None:
    with pytest.raises(RequirementSubmissionError, match="non-whitespace"):
        resolve_inline_requirement(text)


def test_arbitrary_submission_maps_to_one_coarse_intake_requirement() -> None:
    story = "# Audit service\n\n1. Record events.\n2. Query events."
    submission = resolve_inline_requirement(f"  {story}\r\n")
    initial = workflow_input_from_submission(
        submission,
        project_name="audit-service",
    )

    intake = requirements_intake(initial)

    assert initial["raw_requirement"] == story
    assert initial["requirements"] == [story]
    assert intake["raw_requirement"] == story
    assert intake["requirements"] == [story]
    assert intake["normalized_requirements"] == [{"id": "REQ-001", "text": story}]
    assert intake["requirement_submission"] == submission.as_state_data()
    assert intake["project_delivery_policy"] == {
        "mode": ProjectDeliveryMode.RUNNABLE_PROJECT.value
    }


def test_requirement_analysis_receives_normalized_story_through_existing_path() -> None:
    story = "Build a scheduler.\n\n- Detect conflicts."
    submission = resolve_inline_requirement(f"\r\n {story}\r\n")
    analyst = FakeRequirementAnalysisClient([_analysis()])
    workflow = build_workflow(analyst, FakeTaskPlanningClient([]))

    paused = run_workflow(
        workflow_input_from_submission(submission, project_name="scheduler"),
        thread_id="custom-analysis-path",
        workflow=workflow,
    )

    assert paused["__interrupt__"][0].value["stage"] == (
        "requirement_analysis_review"
    )
    assert analyst.calls == [
        {"raw_requirement": story, "prior_analysis": None, "human_feedback": ""}
    ]
    assert paused["normalized_requirements"] == [
        {"id": "REQ-001", "text": story}
    ]


def test_file_change_and_human_feedback_cannot_replace_original_submission(
    tmp_path: Path,
) -> None:
    source = tmp_path / "story.md"
    original = "  Build a calculator.\r\n\r\n- Add two numbers.  \r\n"
    normalized = "Build a calculator.\n\n- Add two numbers."
    source.write_bytes(original.encode())
    submission = resolve_requirement_file(source)
    analyst = FakeRequirementAnalysisClient([_analysis("v1"), _analysis("v2")])
    workflow = build_workflow(analyst, FakeTaskPlanningClient([]))
    thread_id = "immutable-file-submission"

    first_pause = run_workflow(
        workflow_input_from_submission(submission, project_name="calculator"),
        thread_id=thread_id,
        workflow=workflow,
    )
    source.write_text("Build something entirely different.", encoding="utf-8")
    second_pause = resume_workflow(
        thread_id,
        {"decision": "REQUEST_CHANGES", "feedback": "Clarify decimal handling."},
        workflow=workflow,
    )

    assert first_pause["requirement_submission"]["original_text"] == original
    assert second_pause["requirement_submission"]["original_text"] == original
    assert second_pause["requirement_submission"]["normalized_text"] == normalized
    assert second_pause["raw_requirement"] == normalized
    assert second_pause["requirements"] == [normalized]
    assert second_pause["requirement_review_feedback"] == "Clarify decimal handling."
    assert "Clarify decimal handling." not in second_pause["requirement_submission"][
        "original_text"
    ]
    assert analyst.calls[1]["raw_requirement"] == normalized
    assert analyst.calls[1]["human_feedback"] == "Clarify decimal handling."


def test_requirement_evidence_contains_original_submission_metadata(
    tmp_path: Path,
) -> None:
    original = "\ufeff  Build a ledger.\r\n- Record credits.  \r\n"
    submission = resolve_inline_requirement(original)
    analyst = FakeRequirementAnalysisClient([_analysis()])
    workflow = build_workflow(analyst, FakeTaskPlanningClient([]))
    artifact_dir = tmp_path / "evidence"
    thread_id = "submission-evidence"

    run_workflow(
        workflow_input_from_submission(submission, project_name="ledger"),
        thread_id=thread_id,
        artifact_dir=artifact_dir,
        workflow=workflow,
    )
    terminal = resume_workflow(
        thread_id,
        {"decision": "REJECT", "feedback": ""},
        artifact_dir=artifact_dir,
        workflow=workflow,
    )

    evidence = json.loads(
        (artifact_dir / "requirements.json").read_text(encoding="utf-8")
    )
    assert terminal["workflow_status"] == "safe_stopped"
    assert evidence["requirement_submission"] == submission.as_state_data()
    assert evidence["raw_requirement"] == submission.normalized_text
    assert evidence["submitted_requirements"] == [submission.normalized_text]
    assert evidence["normalized_requirements"] == [
        {"id": "REQ-001", "text": submission.normalized_text}
    ]


def test_requirement_analysis_markdown_distinguishes_original_and_normalized() -> None:
    original = "\ufeff  # Ledger\r\n\r\n- Record credits.  \r\n"
    submission = resolve_inline_requirement(original)
    state = _requirement_markdown_state(submission.normalized_text)
    state["requirement_submission"] = submission.as_state_data()

    markdown = _requirement_analysis_markdown(state)

    original_section, normalized_and_analysis = markdown.split(
        "## Normalized workflow requirement", maxsplit=1
    )
    normalized_section = normalized_and_analysis.split(
        "## Current validated analysis", maxsplit=1
    )[0]
    assert "## Original submitted requirement" in original_section
    assert _quoted_requirement(original) in original_section
    assert (
        "The following normalized requirement text entered Requirement Analysis:"
        in normalized_section
    )
    assert _quoted_requirement(submission.normalized_text) in normalized_section
    assert submission.normalized_text == state["raw_requirement"]


def test_requirement_analysis_markdown_does_not_duplicate_identical_submission(
) -> None:
    requirement = "Build a ledger with deterministic reports."
    submission = resolve_inline_requirement(requirement)
    state = _requirement_markdown_state(requirement)
    state["requirement_submission"] = submission.as_state_data()

    markdown = _requirement_analysis_markdown(state)

    assert "## Original submitted requirement" in markdown
    assert "## Normalized workflow requirement" not in markdown
    assert markdown.count(_quoted_requirement(requirement)) == 1


def test_requirement_analysis_markdown_keeps_legacy_state_compatible() -> None:
    raw_requirement = "Legacy scenario requirement.\n\n- Preserve this input."

    markdown = _requirement_analysis_markdown(
        _requirement_markdown_state(raw_requirement)
    )

    assert "## Original requirement" in markdown
    assert _quoted_requirement(raw_requirement) in markdown
    assert "## Original submitted requirement" not in markdown
    assert "## Normalized workflow requirement" not in markdown


def test_cli_accepts_inline_requirement_and_deterministic_default_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _capture_cli_execution(monkeypatch)
    original = "  Create a notes API.\r\n- Store notes.  "
    submission = resolve_inline_requirement(original)

    assert cli.main(["run", "--requirement", original]) == 0

    workflow_input, command, requested_name = calls[0]
    assert command == "run"
    assert requested_name is None
    assert workflow_input["project_name"] == deterministic_project_name(submission)
    assert normalize_project_name(workflow_input["project_name"]) == workflow_input[
        "project_name"
    ]
    assert workflow_input["raw_requirement"] == submission.normalized_text
    assert workflow_input["requirements"] == [submission.normalized_text]


def test_cli_accepts_requirement_file_and_optional_project_name(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "requirement.md"
    source.write_text("# Search\n\nReturn matching records.\n", encoding="utf-8")
    calls = _capture_cli_execution(monkeypatch)

    assert (
        cli.main(
            [
                "run",
                "--requirement-file",
                str(source),
                "--project-name",
                "My Search App",
            ]
        )
        == 0
    )

    workflow_input, command, requested_name = calls[0]
    assert command == "run"
    assert requested_name == "My Search App"
    assert workflow_input["project_name"] == "my-search-app"
    assert workflow_input["requirement_submission"]["source_filename"] == (
        "requirement.md"
    )


def test_cli_demo_command_remains_supported(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _capture_cli_execution(monkeypatch)

    assert cli.main(["demo"]) == 0

    workflow_input, command, requested_name = calls[0]
    assert command == "demo"
    assert requested_name is None
    assert workflow_input["project_name"] == "URL Shortener"
    assert workflow_input["requirements"] == list(DEMO_REQUIREMENTS)


@pytest.mark.parametrize(
    ("arguments", "message"),
    [
        (["run"], "one of the arguments"),
        (
            [
                "run",
                "--requirement",
                "Build one thing.",
                "--requirement-file",
                "other.md",
            ],
            "not allowed with argument",
        ),
        (["run", "--requirement", "  \r\n\t"], "non-whitespace"),
        (
            ["run", "--requirement", "Build safely.", "--project-name", "../escape"],
            "Invalid project name",
        ),
    ],
)
def test_cli_rejects_invalid_arguments_before_workflow_side_effects(
    arguments: list[str],
    message: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.chdir(tmp_path)

    def fail_if_executed(*_args: Any, **_kwargs: Any) -> int:
        raise AssertionError("workflow execution must not begin")

    monkeypatch.setattr(cli, "_execute_workflow", fail_if_executed)

    assert cli.main(arguments) == 2

    assert message in capsys.readouterr().err
    assert not (tmp_path / "runs").exists()
    assert not (tmp_path / "projects").exists()


@pytest.mark.parametrize(
    ("filename", "contents", "message"),
    [
        ("missing.md", None, "does not exist"),
        ("invalid.md", b"\xff\xfe", "not valid UTF-8"),
        ("empty.md", b" \r\n\t", "non-whitespace"),
    ],
)
def test_cli_rejects_invalid_requirement_files_before_side_effects(
    filename: str,
    contents: bytes | None,
    message: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    source = tmp_path / filename
    if contents is not None:
        source.write_bytes(contents)
    monkeypatch.chdir(tmp_path)

    def fail_if_executed(*_args: Any, **_kwargs: Any) -> int:
        raise AssertionError("workflow execution must not begin")

    monkeypatch.setattr(cli, "_execute_workflow", fail_if_executed)

    assert cli.main(["run", "--requirement-file", str(source)]) == 2

    assert message in capsys.readouterr().err
    assert not (tmp_path / "runs").exists()
    assert not (tmp_path / "projects").exists()


def test_cli_rejects_unreadable_requirement_path_before_side_effects(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    source = tmp_path / "requirement-directory"
    source.mkdir()
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        cli,
        "_execute_workflow",
        lambda *_args, **_kwargs: pytest.fail("workflow execution must not begin"),
    )

    assert cli.main(["run", "--requirement-file", str(source)]) == 2

    assert "could not be read" in capsys.readouterr().err
    assert not (tmp_path / "runs").exists()


def test_requirement_submission_state_data_is_deterministic() -> None:
    submission = resolve_inline_requirement("Build a deterministic tool.")
    state = requirements_intake(
        workflow_input_from_submission(submission, project_name="deterministic-tool")
    )
    expected = json.dumps(submission.as_state_data(), sort_keys=True)

    assert json.dumps(state["requirement_submission"], sort_keys=True) == expected
