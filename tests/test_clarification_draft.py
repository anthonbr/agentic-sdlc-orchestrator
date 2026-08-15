"""Tests for ephemeral AI-assisted human clarification drafting."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from pytest import MonkeyPatch
from pydantic import ValidationError

from agentic_sdlc.clarification_draft import (
    CLARIFICATION_DRAFT_PROMPT_VERSION,
    ClarificationDraftRequest,
    ClarificationDraftResult,
    MissingClarificationDraftAPIKeyError,
    OpenAIClarificationDrafter,
    clarification_draft_context_identity,
)
from agentic_sdlc.requirement_analysis import (
    RequirementAnalysis,
    determine_requirement_planning_readiness,
)


def _analysis() -> RequirementAnalysis:
    return RequirementAnalysis(
        normalized_problem_statement="Build a local note-taking API.",
        requirement_type="ambiguous",
        functional_requirements=["Create and list notes."],
        nonfunctional_requirements=[],
        constraints=["Keep the prototype local."],
        ambiguities=[
            "How long should notes be retained?",
            "Should users be able to delete notes?",
            "Is authentication required?",
        ],
        assumptions=[],
        acceptance_criteria=["A user can create and list notes."],
        risks=["Unspecified retention can cause unexpected data loss."],
        needs_clarification=True,
        confidence=0.67,
    )


def _request(
    *,
    gate_token: str = "run-clarification:human-gate:1",
    revision: int = 0,
) -> ClarificationDraftRequest:
    analysis = _analysis()
    return ClarificationDraftRequest(
        run_id="run-clarification",
        gate_token=gate_token,
        analysis_revision=revision,
        original_requirement=(
            "  Build a local note-taking API. Clarify retention, deletion, and "
            "authentication with me.  "
        ),
        requirement_analysis=analysis,
        planning_readiness=determine_requirement_planning_readiness(
            analysis,
            analysis_revision=revision,
        ),
    )


def test_openai_drafter_uses_only_narrow_current_context_without_network() -> None:
    calls: list[dict[str, Any]] = []
    expected = ClarificationDraftResult(
        suggested_clarification=(
            "Retain notes indefinitely unless the user deletes them.\n"
            "Users can delete their own notes.\n"
            "Authentication is not required for this prototype."
        )
    )

    class StubResponses:
        def parse(self, **kwargs: Any) -> SimpleNamespace:
            calls.append(kwargs)
            return SimpleNamespace(output_parsed=expected)

    drafter = OpenAIClarificationDrafter(
        model_name="test-model",
        client=SimpleNamespace(responses=StubResponses()),
    )

    result = drafter.draft(_request(revision=2))

    assert result == expected
    assert len(calls) == 1
    call = calls[0]
    assert call["model"] == "test-model"
    assert call["text_format"] is ClarificationDraftResult
    assert call["store"] is False
    assert len(call["input"]) == 2
    user_context = call["input"][1]["content"]
    assert CLARIFICATION_DRAFT_PROMPT_VERSION in user_context
    assert "Current Requirement Analysis revision: 2" in user_context
    assert "  Build a local note-taking API." in user_context
    assert "Build a local note-taking API." in user_context
    assert "How long should notes be retained?" in user_context
    assert "Should users be able to delete notes?" in user_context
    assert "Is authentication required?" in user_context
    assert "UNRESOLVED_REQUIREMENT_AMBIGUITY" in user_context
    assert "workflow_state" not in user_context


def test_created_openai_drafter_uses_existing_key_convention_and_no_sdk_retry(
    monkeypatch: MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    def fake_openai(**kwargs: Any) -> SimpleNamespace:
        captured.update(kwargs)
        return SimpleNamespace()

    monkeypatch.setattr("agentic_sdlc.clarification_draft.OpenAI", fake_openai)

    OpenAIClarificationDrafter(api_key="test-key")._create_client()

    assert captured == {"api_key": "test-key", "max_retries": 0}


def test_missing_api_key_fails_before_any_provider_request(
    monkeypatch: MonkeyPatch,
) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    with pytest.raises(MissingClarificationDraftAPIKeyError):
        OpenAIClarificationDrafter(api_key="").draft(_request())


def test_request_rejects_non_blocked_or_mismatched_authoritative_context() -> None:
    blocked = _analysis()
    ready = blocked.model_copy(
        update={"ambiguities": [], "needs_clarification": False}
    )

    with pytest.raises(ValidationError, match="blocked Requirement Analysis"):
        ClarificationDraftRequest(
            run_id="run-clarification",
            gate_token="gate-1",
            analysis_revision=0,
            original_requirement="Build it.",
            requirement_analysis=ready,
            planning_readiness=determine_requirement_planning_readiness(
                ready,
                analysis_revision=0,
            ),
        )

    mismatched = determine_requirement_planning_readiness(
        blocked,
        analysis_revision=0,
    ).model_copy(update={"blocking_ambiguities": ("A different question?",)})
    with pytest.raises(ValidationError, match="do not match"):
        ClarificationDraftRequest(
            run_id="run-clarification",
            gate_token="gate-1",
            analysis_revision=0,
            original_requirement="Build it.",
            requirement_analysis=blocked,
            planning_readiness=mismatched,
        )

    with pytest.raises(ValidationError, match="non-whitespace"):
        ClarificationDraftRequest(
            run_id="run-clarification",
            gate_token="gate-1",
            analysis_revision=0,
            original_requirement=" \t\n ",
            requirement_analysis=blocked,
            planning_readiness=determine_requirement_planning_readiness(
                blocked,
                analysis_revision=0,
            ),
        )


def test_context_identity_is_stable_and_changes_with_gate_or_revision() -> None:
    current = _request()

    assert clarification_draft_context_identity(current) == (
        clarification_draft_context_identity(current.model_copy())
    )
    assert clarification_draft_context_identity(current) != (
        clarification_draft_context_identity(_request(gate_token="another-gate"))
    )
    assert clarification_draft_context_identity(current) != (
        clarification_draft_context_identity(_request(revision=1))
    )
