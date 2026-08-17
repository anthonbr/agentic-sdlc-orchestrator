"""Bounded LLM adapter for one governed task-execution request."""

from __future__ import annotations

import json
import os
from typing import Any, Protocol

from openai import (
    APIConnectionError,
    APIResponseValidationError,
    APITimeoutError,
    AuthenticationError,
    BadRequestError,
    ConflictError,
    InternalServerError,
    NotFoundError,
    OpenAI,
    OpenAIError,
    PermissionDeniedError,
    RateLimitError,
    UnprocessableEntityError,
)
from pydantic import ValidationError

from agentic_sdlc.llm import DEFAULT_OPENAI_MODEL
from agentic_sdlc.prompts import TASK_EXECUTION_SYSTEM_PROMPT
from agentic_sdlc.task_execution_contracts import (
    TaskExecutionResult,
)
from agentic_sdlc.workspace_integration_contracts import (
    WorkspaceBoundTaskExecutionRequest,
)


TASK_EXECUTION_REASONING_EFFORT = "xhigh"


class TaskExecutor(Protocol):
    """Narrow boundary from one authoritative request to one semantic result.

    Governed parallel workflows may call ``execute`` concurrently for independent
    attempts. Implementations must therefore be concurrency-safe or provide their
    own synchronization.
    """

    model_name: str

    def execute(
        self, request: WorkspaceBoundTaskExecutionRequest
    ) -> TaskExecutionResult:
        """Propose semantic output for exactly one task attempt."""


class TaskExecutorError(RuntimeError):
    """Raised when no usable structured executor result is available."""

    def __init__(self, message: str, *, retryable: bool) -> None:
        super().__init__(message)
        self.retryable = retryable


class OpenAITaskExecutor:
    """Invoke OpenAI Structured Outputs once for one bounded task request.

    Without an injected client, each invocation creates its own OpenAI client.
    Concurrency safety for a caller-owned injected client remains the caller's
    responsibility.
    """

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

    def execute(
        self, request: WorkspaceBoundTaskExecutionRequest
    ) -> TaskExecutionResult:
        """Return one non-authoritative result without lifecycle side effects."""

        client = self._client or self._create_client()
        try:
            response = client.responses.parse(
                model=self.model_name,
                reasoning={"effort": TASK_EXECUTION_REASONING_EFFORT},
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
                "OpenAI returned a task execution result that failed schema parsing.",
                retryable=True,
            ) from error
        except APIResponseValidationError as error:
            raise TaskExecutorError(
                "OpenAI returned a task execution response that failed SDK "
                "validation.",
                retryable=True,
            ) from error
        except (
            AuthenticationError,
            PermissionDeniedError,
            BadRequestError,
            NotFoundError,
            UnprocessableEntityError,
        ) as error:
            raise TaskExecutorError(
                f"OpenAI task execution was rejected ({type(error).__name__}).",
                retryable=False,
            ) from error
        except (
            RateLimitError,
            APIConnectionError,
            APITimeoutError,
            ConflictError,
            InternalServerError,
        ) as error:
            raise TaskExecutorError(
                f"OpenAI task execution failed transiently "
                f"({type(error).__name__}).",
                retryable=True,
            ) from error
        except OpenAIError as error:
            raise TaskExecutorError(
                f"OpenAI task execution failed ({type(error).__name__}).",
                retryable=False,
            ) from error

        parsed = getattr(response, "output_parsed", None)
        if parsed is None:
            raise TaskExecutorError(
                "OpenAI returned no structured task execution result.",
                retryable=True,
            )
        if isinstance(parsed, TaskExecutionResult):
            return parsed
        try:
            return TaskExecutionResult.model_validate(parsed)
        except ValidationError as error:
            raise TaskExecutorError(
                "OpenAI returned an unexpected structured task execution result.",
                retryable=True,
            ) from error

    def _create_client(self) -> OpenAI:
        api_key = self._api_key or os.getenv("OPENAI_API_KEY", "")
        if not api_key.strip() or api_key == "YOUR_KEY_HERE":
            raise TaskExecutorError(
                "OPENAI_API_KEY is not configured; task execution cannot run.",
                retryable=False,
            )
        return OpenAI(api_key=api_key, max_retries=0)


def build_task_execution_input(request: WorkspaceBoundTaskExecutionRequest) -> str:
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
        "application_retry_context": (
            request.retry_context.model_dump(mode="json")
            if request.retry_context is not None
            else None
        ),
        "workspace_binding": request.workspace_binding.model_dump(mode="json"),
        "repository_context": request.repository_context.model_dump(mode="json"),
    }
    return json.dumps(payload, sort_keys=True, indent=2, ensure_ascii=False)
