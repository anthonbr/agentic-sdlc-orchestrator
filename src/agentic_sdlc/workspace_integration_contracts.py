"""Immutable vocabulary for governed task-to-workspace integration.

These checkpoint-safe contracts grant no filesystem capability. Application
runtime code separately provides bounded read and isolated mutation authority.
"""

from __future__ import annotations

import hashlib
import json
from enum import StrEnum
from typing import Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from agentic_sdlc.task_execution_contracts import (
    EngineeringArtifact,
    TaskExecutionRequest,
    TaskExecutionRetryContext,
    TaskRequirementContext,
)
from agentic_sdlc.task_graph import Task, TaskMaterializationPolicy
from agentic_sdlc.workspace_contracts import (
    ArtifactMaterializationIntent,
    ArtifactMaterializationIssueCode,
    ArtifactMaterializationValidationIssue,
    ArtifactMaterializationValidationResult,
    WorkspaceChangeSetConflictAnalysis,
    normalize_repository_path,
    workspace_file_content_hash,
)


class WorkspaceIntegrationContractError(ValueError):
    """Raised when canonical integration contracts cannot be constructed."""


class WorkspaceBinding(BaseModel):
    """Exact isolated workspace snapshot against which one attempt reasons."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    workspace_id: str = Field(min_length=1)
    snapshot_id: str = Field(min_length=1)


class RepositoryPathObservation(BaseModel):
    """Exact observed state of one approved repository-relative regular path."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    path: str
    exists: bool
    content: str | None
    content_hash: str | None

    @field_validator("path")
    @classmethod
    def validate_path(cls, value: str) -> str:
        return normalize_repository_path(value)

    @model_validator(mode="after")
    def validate_observed_state(self) -> Self:
        if not self.exists:
            if self.content is not None or self.content_hash is not None:
                raise ValueError(
                    "A nonexistent repository path cannot carry content or a hash."
                )
            return self
        if self.content is None or self.content_hash is None:
            raise ValueError(
                "An existing repository file requires complete content and its hash."
            )
        if self.content_hash != workspace_file_content_hash(self.content):
            raise ValueError(
                "Repository observation content_hash must match complete content."
            )
        return self


class RepositoryContext(BaseModel):
    """Canonical bounded projection of exact repository evidence for an attempt."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    repository_context_id: str = Field(min_length=1)
    binding: WorkspaceBinding
    observations: tuple[RepositoryPathObservation, ...]

    @model_validator(mode="after")
    def validate_canonical_context(self) -> Self:
        paths = tuple(item.path for item in self.observations)
        if paths != tuple(sorted(paths)):
            raise ValueError("Repository observations must be in canonical path order.")
        if len(paths) != len(set(paths)):
            raise ValueError("Repository observation paths must be unique.")
        if not repository_context_identity_is_valid(self):
            raise ValueError(
                "Repository context identity does not match its canonical contents."
            )
        return self


class TaskAttemptExitDisposition(StrEnum):
    """Finite governed outcomes available to a future task-attempt exit gate."""

    SUCCEED_TASK = "SUCCEED_TASK"
    RETRY_TASK = "RETRY_TASK"
    FAIL_TASK = "FAIL_TASK"
    SAFE_STOP_RUN = "SAFE_STOP_RUN"


class TaskAttemptExitDecision(BaseModel):
    """Immutable future exit-gate decision for one correlated task attempt."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    task_id: str = Field(min_length=1)
    attempt_number: int = Field(ge=1)
    request_id: str | None
    attempt_id: str | None
    disposition: TaskAttemptExitDisposition
    reason_code: str = Field(min_length=1, pattern=r"^[A-Z][A-Z0-9_]*$")
    evidence_ids: tuple[str, ...] = ()

    @field_validator("evidence_ids")
    @classmethod
    def validate_evidence_ids(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if any(not value for value in values):
            raise ValueError("Task-attempt exit evidence IDs must be non-empty.")
        if values != tuple(sorted(values)) or len(values) != len(set(values)):
            raise ValueError(
                "Task-attempt exit evidence IDs must be unique and canonical."
            )
        return values


class WorkspaceIntegrityStatus(StrEnum):
    """Whether authoritative workspace integrity is proved after mutation work."""

    VERIFIED = "VERIFIED"
    UNPROVABLE = "UNPROVABLE"


class WorkspaceDispatchMode(StrEnum):
    """Finite scheduler modes for normal waves and serialized conflict fallback."""

    PARALLEL = "PARALLEL"
    SERIALIZED_CONFLICT_RETRY = "SERIALIZED_CONFLICT_RETRY"


class GovernedWorkspaceSession(BaseModel):
    """Immutable authority record for one run's isolated workspace state."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    run_id: str = Field(min_length=1)
    workspace_id: str = Field(min_length=1)
    baseline_snapshot_id: str = Field(min_length=1)
    authoritative_snapshot_id: str = Field(min_length=1)
    integrity_status: WorkspaceIntegrityStatus


class WorkspaceExecutionWave(BaseModel):
    """Immutable binding shared by every attempt in one authorized wave."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    wave_number: int = Field(ge=1)
    dispatch_mode: WorkspaceDispatchMode
    binding: WorkspaceBinding


class WorkspaceWaveConflictEvidence(BaseModel):
    """Deterministic same-wave write/write reconciliation evidence."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    conflict_evidence_id: str
    wave_number: int = Field(ge=1)
    analysis: WorkspaceChangeSetConflictAnalysis


class WorkspaceBoundTaskExecutionRequest(BaseModel):
    """Strict request wrapper bound to exact immutable repository evidence."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    request: TaskExecutionRequest
    workspace_binding: WorkspaceBinding
    repository_context: RepositoryContext

    @model_validator(mode="after")
    def validate_binding(self) -> Self:
        if self.repository_context.binding != self.workspace_binding:
            raise ValueError(
                "Repository context and workspace request binding must match."
            )
        return self

    @property
    def request_id(self) -> str:
        return self.request.request_id

    @property
    def attempt_id(self) -> str:
        return self.request.attempt_id

    @property
    def task_id(self) -> str:
        return self.request.task_id

    @property
    def attempt_number(self) -> int:
        return self.request.attempt_number

    @property
    def task(self) -> Task:
        return self.request.task

    @property
    def requirement_context(self) -> TaskRequirementContext:
        return self.request.requirement_context

    @property
    def dependency_artifacts(self) -> tuple[EngineeringArtifact, ...]:
        return self.request.dependency_artifacts

    @property
    def retry_context(self) -> TaskExecutionRetryContext | None:
        return self.request.retry_context


def build_repository_context(
    binding: WorkspaceBinding,
    observations: tuple[RepositoryPathObservation, ...] = (),
) -> RepositoryContext:
    """Build a deterministic bounded repository projection without filesystem I/O."""

    ordered = tuple(sorted(observations, key=lambda item: item.path))
    paths = tuple(item.path for item in ordered)
    if len(paths) != len(set(paths)):
        raise WorkspaceIntegrationContractError(
            "Repository observation paths must be unique."
        )
    return RepositoryContext(
        repository_context_id=_repository_context_id(binding, ordered),
        binding=binding,
        observations=ordered,
    )


def build_workspace_bound_task_execution_request(
    request: TaskExecutionRequest,
    workspace_binding: WorkspaceBinding,
    repository_context: RepositoryContext,
) -> WorkspaceBoundTaskExecutionRequest:
    """Bind an unchanged task request to exact repository evidence."""

    return WorkspaceBoundTaskExecutionRequest(
        request=request,
        workspace_binding=workspace_binding,
        repository_context=repository_context,
    )


def repository_context_identity_is_valid(context: RepositoryContext) -> bool:
    """Return whether a context ID still binds its exact canonical evidence."""

    return context.repository_context_id == _repository_context_id(
        context.binding, context.observations
    )


def build_workspace_wave_conflict_evidence(
    wave_number: int,
    analysis: WorkspaceChangeSetConflictAnalysis,
) -> WorkspaceWaveConflictEvidence:
    """Bind deterministic conflict analysis to its execution wave."""

    payload = {
        "wave_number": wave_number,
        "analysis": analysis.model_dump(mode="json"),
    }
    digest = hashlib.sha256(
        json.dumps(
            payload,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode("utf-8")
    ).hexdigest()
    return WorkspaceWaveConflictEvidence(
        conflict_evidence_id=f"WORKSPACE-CONFLICT-{digest[:12].upper()}",
        wave_number=wave_number,
        analysis=analysis,
    )


def _repository_context_id(
    binding: WorkspaceBinding,
    observations: tuple[RepositoryPathObservation, ...],
) -> str:
    payload = {
        "binding": binding.model_dump(mode="json"),
        "observations": [item.model_dump(mode="json") for item in observations],
    }
    digest = hashlib.sha256(
        json.dumps(
            payload,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode("utf-8")
    ).hexdigest()
    return f"REPOSITORY-CONTEXT-{digest[:12].upper()}"
