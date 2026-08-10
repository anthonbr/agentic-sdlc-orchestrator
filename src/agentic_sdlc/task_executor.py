"""Bounded LLM adapter for one governed task-execution request."""

from __future__ import annotations

import json
import os
from typing import Any, Protocol

from openai import (
    AuthenticationError,
    BadRequestError,
    OpenAI,
    OpenAIError,
    PermissionDeniedError,
)
from pydantic import ValidationError

from agentic_sdlc.llm import DEFAULT_OPENAI_MODEL
from agentic_sdlc.prompts import TASK_EXECUTION_SYSTEM_PROMPT
from agentic_sdlc.task_execution_contracts import (
    TaskExecutionRequest,
    TaskExecutionResult,
)


class TaskExecutor(Protocol):
    """Narrow boundary from one authoritative request to one semantic result."""

    model_name: str

    def execute(self, request: TaskExecutionRequest) -> TaskExecutionResult:
        """Propose semantic output for exactly one task attempt."""


class TaskExecutorError(RuntimeError):
    """Raised when no usable structured executor result is available."""


class OpenAITaskExecutor:
    """Invoke OpenAI Structured Outputs once for one bounded task request."""

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

    def execute(self, request: TaskExecutionRequest) -> TaskExecutionResult:
        """Return one non-authoritative result without lifecycle side effects."""

        client = self._client or self._create_client()
        try:
            response = client.responses.parse(
                model=self.model_name,
                input=[
                    {"role": "system", "content": TASK_EXECUTION_SYSTEM_PROMPT},
                    {
                        "role": "user",
                        "content": build_task_execution_input(request),
                    },
                ],
                text_format=TaskExecutionResult,
                store=False,
            )
        except ValidationError as error:
            raise TaskExecutorError(
                "OpenAI returned a task execution result that failed schema parsing."
            ) from error
        except (AuthenticationError, PermissionDeniedError, BadRequestError) as error:
            raise TaskExecutorError(
                f"OpenAI task execution was rejected ({type(error).__name__})."
            ) from error
        except OpenAIError as error:
            raise TaskExecutorError(
                f"OpenAI task execution failed ({type(error).__name__})."
            ) from error

        parsed = getattr(response, "output_parsed", None)
        if parsed is None:
            raise TaskExecutorError(
                "OpenAI returned no structured task execution result."
            )
        if isinstance(parsed, TaskExecutionResult):
            return parsed
        try:
            return TaskExecutionResult.model_validate(parsed)
        except ValidationError as error:
            raise TaskExecutorError(
                "OpenAI returned an unexpected structured task execution result."
            ) from error

    def _create_client(self) -> OpenAI:
        api_key = self._api_key or os.getenv("OPENAI_API_KEY", "")
        if not api_key.strip() or api_key == "YOUR_KEY_HERE":
            raise TaskExecutorError(
                "OPENAI_API_KEY is not configured; task execution cannot run."
            )
        return OpenAI(api_key=api_key)


def build_task_execution_input(request: TaskExecutionRequest) -> str:
    """Serialize only authoritative bounded request context deterministically."""

    payload = {
        "correlation_identifiers": {
            "request_id": request.request_id,
            "attempt_id": request.attempt_id,
            "task_id": request.task_id,
            "instruction": (
                "Echo these identifiers exactly; do not generate or modify them."
            ),
        },
        "approved_requirement_context": request.requirement_context.model_dump(
            mode="json"
        ),
        "canonical_task": request.task.model_dump(mode="json"),
        "accepted_direct_dependency_artifacts": [
            artifact.model_dump(mode="json")
            for artifact in request.dependency_artifacts
        ],
    }
    return json.dumps(payload, sort_keys=True, indent=2, ensure_ascii=False)
