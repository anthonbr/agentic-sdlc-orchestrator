"""Application-owned policy for the shape of a governed project deliverable."""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict


class ProjectDeliveryMode(StrEnum):
    """Minimum final-project contract selected by the orchestration application."""

    ENGINEERING_ARTIFACTS = "ENGINEERING_ARTIFACTS"
    RUNNABLE_PROJECT = "RUNNABLE_PROJECT"


class ProjectDeliverableRole(StrEnum):
    """Structured, human-reviewable responsibilities assigned to canonical tasks."""

    RUNNABLE_ENTRYPOINT = "RUNNABLE_ENTRYPOINT"
    AUTOMATED_TESTS = "AUTOMATED_TESTS"
    RUN_INSTRUCTIONS = "RUN_INSTRUCTIONS"


RUNNABLE_PROJECT_REQUIRED_ROLES = (
    ProjectDeliverableRole.RUNNABLE_ENTRYPOINT,
    ProjectDeliverableRole.AUTOMATED_TESTS,
    ProjectDeliverableRole.RUN_INSTRUCTIONS,
)


class ProjectDeliveryPolicy(BaseModel):
    """Immutable application context kept separate from approved requirements."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    mode: ProjectDeliveryMode = ProjectDeliveryMode.ENGINEERING_ARTIFACTS

    @property
    def required_roles(self) -> tuple[ProjectDeliverableRole, ...]:
        """Return deterministic role postconditions for this delivery mode."""

        if self.mode is ProjectDeliveryMode.RUNNABLE_PROJECT:
            return RUNNABLE_PROJECT_REQUIRED_ROLES
        return ()


DEFAULT_PROJECT_DELIVERY_POLICY = ProjectDeliveryPolicy()
RUNNABLE_PROJECT_DELIVERY_POLICY = ProjectDeliveryPolicy(
    mode=ProjectDeliveryMode.RUNNABLE_PROJECT
)


def project_delivery_policy_from_value(value: object | None) -> ProjectDeliveryPolicy:
    """Parse application context, defaulting only when no policy was supplied."""

    if value is None:
        return DEFAULT_PROJECT_DELIVERY_POLICY
    if isinstance(value, ProjectDeliveryPolicy):
        return value
    if isinstance(value, dict) and set(value) == {"mode"}:
        mode = value["mode"]
        if isinstance(mode, str):
            return ProjectDeliveryPolicy(mode=ProjectDeliveryMode(mode))
    return ProjectDeliveryPolicy.model_validate(value)
