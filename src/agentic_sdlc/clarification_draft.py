"""Ephemeral application assistance for drafting human clarification text."""

from __future__ import annotations

import hashlib
import json
import os
from collections import deque
from collections.abc import Iterable
from typing import Any, Protocol, Self

from openai import (
    AuthenticationError,
    BadRequestError,
    OpenAI,
    OpenAIError,
    PermissionDeniedError,
)
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
    model_validator,
)

from agentic_sdlc.llm import DEFAULT_OPENAI_MODEL
from agentic_sdlc.requirement_analysis import (
    RequirementAnalysis,
    RequirementPlanningReadiness,
    RequirementPlanningReadinessStatus,
)


CLARIFICATION_DRAFT_PROMPT_VERSION = "clarification-draft-v1"

CLARIFICATION_DRAFT_SYSTEM_PROMPT = """\
You draft proposed human clarification text for a governed software requirement
review. Answer each blocking ambiguity directly and concisely, preferably with one
answer per ambiguity. Stay within the original user request where possible. Do not
restate the questions, add unrelated implementation detail, claim authority, make
an approval decision, or describe workflow actions. Return only clarification text
that a human can edit before explicitly submitting it.
"""


class ClarificationDraftRequest(BaseModel):
    """Exact current blocked-analysis context permitted for drafting assistance."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    run_id: str = Field(min_length=1)
    gate_token: str = Field(min_length=1)
    analysis_revision: int = Field(ge=0)
    original_requirement: str = Field(min_length=1)
    requirement_analysis: RequirementAnalysis
    planning_readiness: RequirementPlanningReadiness

    @field_validator("original_requirement")
    @classmethod
    def _require_nonblank_original_text(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("Original requirement must contain non-whitespace text.")
        return value

    @model_validator(mode="after")
    def _require_current_blocked_context(self) -> Self:
        if self.planning_readiness.analysis_revision != self.analysis_revision:
            raise ValueError("Clarification draft revision context is inconsistent.")
        if (
            self.planning_readiness.status
            is not RequirementPlanningReadinessStatus.BLOCKED
            or not self.planning_readiness.blocking_ambiguities
            or not self.requirement_analysis.needs_clarification
        ):
            raise ValueError(
                "Clarification drafting requires a blocked Requirement Analysis."
            )
        if tuple(self.requirement_analysis.ambiguities) != (
            self.planning_readiness.blocking_ambiguities
        ):
            raise ValueError(
                "Blocking ambiguities do not match the current Requirement Analysis."
            )
        return self


class ClarificationDraftResult(BaseModel):
    """Editable, non-authoritative text returned by one explicit draft request."""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        strict=True,
        str_strip_whitespace=True,
    )

    suggested_clarification: str = Field(min_length=1, max_length=6000)


class ClarificationDrafter(Protocol):
    """Narrow synchronous presentation-assistance boundary."""

    model_name: str

    def draft(self, request: ClarificationDraftRequest) -> ClarificationDraftResult:
        """Return editable proposed text without invoking workflow authority."""


class ClarificationDraftError(RuntimeError):
    """Safe clarification-provider failure for presentation rendering."""


class MissingClarificationDraftAPIKeyError(ClarificationDraftError):
    """Raised when an explicit draft request has no usable credential."""

    def __init__(self) -> None:
        super().__init__(
            "OPENAI_API_KEY is not configured; clarification drafting cannot run."
        )


class OpenAIClarificationDrafter:
    """Make one structured OpenAI call after an explicit human UI action."""

    def __init__(
        self,
        *,
        model_name: str | None = None,
        api_key: str | None = None,
        client: Any | None = None,
    ) -> None:
        self.model_name = (
            model_name or os.getenv("OPENAI_MODEL") or DEFAULT_OPENAI_MODEL
        )
        self._api_key = api_key
        self._client = client

    def draft(self, request: ClarificationDraftRequest) -> ClarificationDraftResult:
        """Return a bounded structured suggestion without any governed side effect."""

        client = self._client or self._create_client()
        try:
            response = client.responses.parse(
                model=self.model_name,
                input=[
                    {"role": "system", "content": CLARIFICATION_DRAFT_SYSTEM_PROMPT},
                    {"role": "user", "content": _clarification_draft_input(request)},
                ],
                text_format=ClarificationDraftResult,
                store=False,
            )
        except ValidationError as error:
            raise ClarificationDraftError(
                "OpenAI returned a clarification draft that failed schema parsing."
            ) from error
        except (AuthenticationError, PermissionDeniedError, BadRequestError) as error:
            raise ClarificationDraftError(
                f"OpenAI clarification drafting was rejected ({type(error).__name__})."
            ) from error
        except OpenAIError as error:
            raise ClarificationDraftError(
                f"OpenAI clarification drafting failed ({type(error).__name__})."
            ) from error

        result = response.output_parsed
        if not isinstance(result, ClarificationDraftResult):
            raise ClarificationDraftError(
                "OpenAI returned no structured clarification draft."
            )
        return result

    def _create_client(self) -> OpenAI:
        api_key = self._api_key or os.getenv("OPENAI_API_KEY", "")
        if not api_key.strip() or api_key == "YOUR_KEY_HERE":
            raise MissingClarificationDraftAPIKeyError()
        return OpenAI(api_key=api_key, max_retries=0)


class FakeClarificationDrafter:
    """Scripted deterministic drafter for network-free presentation tests."""

    def __init__(
        self,
        responses: Iterable[ClarificationDraftResult | ClarificationDraftError],
        *,
        model_name: str = "fake-clarification-drafter",
    ) -> None:
        self.model_name = model_name
        self._responses = deque(responses)
        self.calls: list[ClarificationDraftRequest] = []

    def draft(self, request: ClarificationDraftRequest) -> ClarificationDraftResult:
        self.calls.append(request)
        if not self._responses:
            raise AssertionError("No scripted clarification draft response remains.")
        response = self._responses.popleft()
        if isinstance(response, ClarificationDraftError):
            raise response
        return response


def clarification_draft_context_identity(request: ClarificationDraftRequest) -> str:
    """Bind ephemeral UI text to one exact governed gate and analysis revision."""

    payload = {
        "run_id": request.run_id,
        "gate_token": request.gate_token,
        "analysis_revision": request.analysis_revision,
        "original_requirement_sha256": hashlib.sha256(
            request.original_requirement.encode("utf-8")
        ).hexdigest(),
        "requirement_analysis": request.requirement_analysis.model_dump(mode="json"),
        "planning_readiness": request.planning_readiness.model_dump(mode="json"),
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return "CLARIFICATION-DRAFT-CONTEXT-" + hashlib.sha256(
        canonical.encode("utf-8")
    ).hexdigest()[:20].upper()


def _clarification_draft_input(request: ClarificationDraftRequest) -> str:
    analysis_json = json.dumps(
        request.requirement_analysis.model_dump(mode="json"),
        indent=2,
        sort_keys=True,
    )
    readiness_json = json.dumps(
        request.planning_readiness.model_dump(mode="json"),
        indent=2,
        sort_keys=True,
    )
    blocking_questions = "\n".join(
        f"{index}. {question}"
        for index, question in enumerate(
            request.planning_readiness.blocking_ambiguities,
            start=1,
        )
    )
    return "\n".join(
        (
            f"Prompt version: {CLARIFICATION_DRAFT_PROMPT_VERSION}",
            f"Current Requirement Analysis revision: {request.analysis_revision}",
            "",
            "Original raw user requirement:",
            request.original_requirement,
            "",
            "Current authoritative Requirement Analysis:",
            analysis_json,
            "",
            "Current deterministic planning-readiness result:",
            readiness_json,
            "",
            "Exact blocking ambiguities to answer:",
            blocking_questions,
            "",
            "Draft concise proposed human clarification answers only.",
        )
    )
