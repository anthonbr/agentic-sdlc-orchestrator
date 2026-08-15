"""Small structured LLM boundaries and OpenAI implementations."""

from __future__ import annotations

import json
import os
from collections import deque
from collections.abc import Iterable
from typing import Any, Protocol

from openai import (
    AuthenticationError,
    BadRequestError,
    OpenAI,
    OpenAIError,
    PermissionDeniedError,
)
from pydantic import ValidationError

from agentic_sdlc.brownfield_context import BrownfieldCodebaseContext
from agentic_sdlc.prompts import (
    REQUIREMENT_ANALYSIS_SYSTEM_PROMPT,
    TASK_PLANNING_SYSTEM_PROMPT,
)
from agentic_sdlc.project_delivery import (
    DEFAULT_PROJECT_DELIVERY_POLICY,
    ProjectDeliveryPolicy,
)
from agentic_sdlc.requirement_analysis import RequirementAnalysis
from agentic_sdlc.requirement_spec import ApprovedRequirementSpec
from agentic_sdlc.task_graph import ProposedTaskGraph, TaskGraph


DEFAULT_OPENAI_MODEL = "gpt-5.6-sol"


class RequirementAnalysisClient(Protocol):
    """Minimal structured invocation boundary used by the workflow node."""

    model_name: str

    def invoke_structured(
        self,
        raw_requirement: str,
        prior_analysis: RequirementAnalysis | None,
        human_feedback: str,
        brownfield_codebase_context: BrownfieldCodebaseContext | None = None,
    ) -> object:
        """Return a candidate that the workflow will validate."""


class RequirementAnalysisClientError(RuntimeError):
    """Safe provider error with deterministic retry guidance."""

    def __init__(self, message: str, *, retryable: bool) -> None:
        super().__init__(message)
        self.retryable = retryable


class MissingOpenAIAPIKeyError(RequirementAnalysisClientError):
    """Raised when runtime analysis has no usable OpenAI credential."""

    def __init__(self) -> None:
        super().__init__(
            "OPENAI_API_KEY is not configured; requirement analysis cannot run.",
            retryable=False,
        )


class OpenAIRequirementAnalysisClient:
    """Invoke OpenAI Structured Outputs for one requirement-analysis task."""

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

    def invoke_structured(
        self,
        raw_requirement: str,
        prior_analysis: RequirementAnalysis | None,
        human_feedback: str,
        brownfield_codebase_context: BrownfieldCodebaseContext | None = None,
    ) -> object:
        """Return the SDK-parsed Pydantic result without making routing decisions."""

        client = self._client or self._create_client()
        try:
            response = client.responses.parse(
                model=self.model_name,
                input=[
                    {"role": "system", "content": REQUIREMENT_ANALYSIS_SYSTEM_PROMPT},
                    {
                        "role": "user",
                        "content": _requirement_analysis_input(
                            raw_requirement,
                            prior_analysis,
                            human_feedback,
                            brownfield_codebase_context,
                        ),
                    },
                ],
                text_format=RequirementAnalysis,
                store=False,
            )
        except ValidationError as error:
            raise RequirementAnalysisClientError(
                "OpenAI returned a requirement analysis that failed schema parsing.",
                retryable=True,
            ) from error
        except (AuthenticationError, PermissionDeniedError, BadRequestError) as error:
            raise RequirementAnalysisClientError(
                f"OpenAI requirement analysis was rejected ({type(error).__name__}).",
                retryable=False,
            ) from error
        except OpenAIError as error:
            raise RequirementAnalysisClientError(
                f"OpenAI requirement analysis failed ({type(error).__name__}).",
                retryable=True,
            ) from error

        if response.output_parsed is None:
            raise RequirementAnalysisClientError(
                "OpenAI returned no structured requirement analysis.",
                retryable=True,
            )
        return response.output_parsed

    def _create_client(self) -> OpenAI:
        api_key = self._api_key or os.getenv("OPENAI_API_KEY", "")
        if not api_key.strip() or api_key == "YOUR_KEY_HERE":
            raise MissingOpenAIAPIKeyError()
        return OpenAI(api_key=api_key, max_retries=0)


class FakeRequirementAnalysisClient:
    """Scripted structured client for deterministic, network-free tests."""

    def __init__(
        self,
        responses: Iterable[object | RequirementAnalysisClientError],
        *,
        model_name: str = "fake-requirement-analyst",
    ) -> None:
        self.model_name = model_name
        self._responses = deque(responses)
        self.calls: list[dict[str, object]] = []

    def invoke_structured(
        self,
        raw_requirement: str,
        prior_analysis: RequirementAnalysis | None,
        human_feedback: str,
        brownfield_codebase_context: BrownfieldCodebaseContext | None = None,
    ) -> object:
        """Return the next scripted response and record revision context."""

        self.calls.append(
            {
                "raw_requirement": raw_requirement,
                "prior_analysis": prior_analysis,
                "human_feedback": human_feedback,
                "brownfield_codebase_context": brownfield_codebase_context,
            }
        )
        if not self._responses:
            raise AssertionError("No scripted requirement-analysis response remains.")
        response = self._responses.popleft()
        if isinstance(response, RequirementAnalysisClientError):
            raise response
        return response


class TaskPlanningClient(Protocol):
    """Minimal structured invocation boundary for task-graph proposals."""

    model_name: str

    def invoke_structured(
        self,
        approved_spec: ApprovedRequirementSpec,
        prior_task_graph: TaskGraph | None,
        human_feedback: str,
        delivery_policy: ProjectDeliveryPolicy = DEFAULT_PROJECT_DELIVERY_POLICY,
        brownfield_codebase_context: BrownfieldCodebaseContext | None = None,
    ) -> object:
        """Return a proposal that deterministic workflow code will validate."""


class TaskPlanningClientError(RuntimeError):
    """Safe task-planning provider error with deterministic retry guidance."""

    def __init__(self, message: str, *, retryable: bool) -> None:
        super().__init__(message)
        self.retryable = retryable


class OpenAITaskPlanningClient:
    """Invoke OpenAI Structured Outputs for one task-graph proposal."""

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

    def invoke_structured(
        self,
        approved_spec: ApprovedRequirementSpec,
        prior_task_graph: TaskGraph | None,
        human_feedback: str,
        delivery_policy: ProjectDeliveryPolicy = DEFAULT_PROJECT_DELIVERY_POLICY,
        brownfield_codebase_context: BrownfieldCodebaseContext | None = None,
    ) -> object:
        """Return an SDK-parsed semantic proposal without assigning authority."""

        client = self._client or self._create_client()
        try:
            response = client.responses.parse(
                model=self.model_name,
                input=[
                    {"role": "system", "content": TASK_PLANNING_SYSTEM_PROMPT},
                    {
                        "role": "user",
                        "content": _task_planning_input(
                            approved_spec,
                            delivery_policy,
                            prior_task_graph,
                            human_feedback,
                            brownfield_codebase_context,
                        ),
                    },
                ],
                text_format=ProposedTaskGraph,
                store=False,
            )
        except ValidationError as error:
            raise TaskPlanningClientError(
                "OpenAI returned a task proposal that failed schema parsing.",
                retryable=True,
            ) from error
        except (AuthenticationError, PermissionDeniedError, BadRequestError) as error:
            raise TaskPlanningClientError(
                f"OpenAI task planning was rejected ({type(error).__name__}).",
                retryable=False,
            ) from error
        except OpenAIError as error:
            raise TaskPlanningClientError(
                f"OpenAI task planning failed ({type(error).__name__}).",
                retryable=True,
            ) from error

        if response.output_parsed is None:
            raise TaskPlanningClientError(
                "OpenAI returned no structured task proposal.", retryable=True
            )
        return response.output_parsed

    def _create_client(self) -> OpenAI:
        api_key = self._api_key or os.getenv("OPENAI_API_KEY", "")
        if not api_key.strip() or api_key == "YOUR_KEY_HERE":
            raise TaskPlanningClientError(
                "OPENAI_API_KEY is not configured; task planning cannot run.",
                retryable=False,
            )
        return OpenAI(api_key=api_key, max_retries=0)


class FakeTaskPlanningClient:
    """Scripted task planner for deterministic, network-free tests."""

    def __init__(
        self,
        responses: Iterable[object | TaskPlanningClientError],
        *,
        model_name: str = "fake-task-planner",
    ) -> None:
        self.model_name = model_name
        self._responses = deque(responses)
        self.calls: list[dict[str, object]] = []

    def invoke_structured(
        self,
        approved_spec: ApprovedRequirementSpec,
        prior_task_graph: TaskGraph | None,
        human_feedback: str,
        delivery_policy: ProjectDeliveryPolicy = DEFAULT_PROJECT_DELIVERY_POLICY,
        brownfield_codebase_context: BrownfieldCodebaseContext | None = None,
    ) -> object:
        self.calls.append(
            {
                "approved_spec": approved_spec,
                "delivery_policy": delivery_policy,
                "prior_task_graph": prior_task_graph,
                "human_feedback": human_feedback,
                "brownfield_codebase_context": brownfield_codebase_context,
            }
        )
        if not self._responses:
            raise AssertionError("No scripted task-planning response remains.")
        response = self._responses.popleft()
        if isinstance(response, TaskPlanningClientError):
            raise response
        return response


def _requirement_analysis_input(
    raw_requirement: str,
    prior_analysis: RequirementAnalysis | None,
    human_feedback: str,
    brownfield_codebase_context: BrownfieldCodebaseContext | None = None,
) -> str:
    sections = ["Raw requirement:", raw_requirement]
    if brownfield_codebase_context is not None:
        sections.extend(
            [
                "",
                "Authoritative bounded brownfield codebase context:",
                json.dumps(
                    brownfield_codebase_context.model_dump(mode="json"),
                    indent=2,
                ),
            ]
        )
    if prior_analysis is not None:
        sections.extend(
            [
                "",
                "Prior validated analysis:",
                json.dumps(prior_analysis.model_dump(mode="json"), indent=2),
            ]
        )
    if human_feedback:
        sections.extend(
            ["", "Authoritative human review feedback:", human_feedback]
        )
    return "\n".join(sections)


def _task_planning_input(
    approved_spec: ApprovedRequirementSpec,
    delivery_policy: ProjectDeliveryPolicy,
    prior_task_graph: TaskGraph | None,
    human_feedback: str,
    brownfield_codebase_context: BrownfieldCodebaseContext | None = None,
) -> str:
    sections = [
        "Human-approved requirement specification:",
        json.dumps(approved_spec.model_dump(mode="json"), indent=2),
        "",
        "Authoritative application-owned project delivery policy:",
        json.dumps(delivery_policy.model_dump(mode="json"), indent=2),
    ]
    if brownfield_codebase_context is not None:
        sections.extend(
            [
                "",
                "Authoritative bounded brownfield codebase context:",
                json.dumps(
                    brownfield_codebase_context.model_dump(mode="json"),
                    indent=2,
                ),
            ]
        )
    if prior_task_graph is not None:
        sections.extend(
            [
                "",
                "Prior validated task graph:",
                json.dumps(prior_task_graph.model_dump(mode="json"), indent=2),
            ]
        )
    if human_feedback:
        sections.extend(
            ["", "Authoritative human task-graph review feedback:", human_feedback]
        )
    return "\n".join(sections)
