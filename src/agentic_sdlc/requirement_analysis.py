"""Validated structured output for governed requirement analysis."""

from __future__ import annotations

from enum import StrEnum
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class RequirementPlanningReadinessStatus(StrEnum):
    """Deterministic permission for an analysis revision to feed planning."""

    READY = "READY"
    BLOCKED = "BLOCKED"


class RequirementPlanningReadinessReasonCode(StrEnum):
    """Machine-readable reason that requirement planning is prohibited."""

    UNRESOLVED_REQUIREMENT_AMBIGUITY = "UNRESOLVED_REQUIREMENT_AMBIGUITY"


class RequirementPlanningReadiness(BaseModel):
    """Immutable readiness decision for one validated analysis revision."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    analysis_revision: int = Field(ge=0)
    status: RequirementPlanningReadinessStatus
    needs_clarification: bool
    blocking_ambiguities: tuple[str, ...]
    reason_code: RequirementPlanningReadinessReasonCode | None


class RequirementPlanningReadinessError(ValueError):
    """Raised when blocked requirement state is asked to feed planning."""


class RequirementAnalysis(BaseModel):
    """Engineering understanding proposed by the requirement-analysis LLM."""

    model_config = ConfigDict(extra="forbid", strict=True, str_strip_whitespace=True)

    normalized_problem_statement: str = Field(min_length=1)
    requirement_type: Literal["greenfield", "brownfield", "ambiguous"]
    functional_requirements: list[str]
    nonfunctional_requirements: list[str]
    constraints: list[str]
    ambiguities: list[str]
    assumptions: list[str]
    acceptance_criteria: list[str]
    risks: list[str]
    needs_clarification: bool
    confidence: float = Field(ge=0.0, le=1.0)

    @field_validator(
        "functional_requirements",
        "nonfunctional_requirements",
        "constraints",
        "ambiguities",
        "assumptions",
        "acceptance_criteria",
        "risks",
    )
    @classmethod
    def reject_blank_collection_items(cls, values: list[str]) -> list[str]:
        """Keep collections predictable and prevent empty apparent findings."""

        stripped_values = [value.strip() for value in values]
        if any(not value for value in stripped_values):
            raise ValueError("collection items must be non-empty text")
        return stripped_values

    @field_validator("functional_requirements", "acceptance_criteria")
    @classmethod
    def require_engineering_content(cls, values: list[str]) -> list[str]:
        """Require the minimum content needed for downstream human review."""

        if not values:
            raise ValueError("at least one item is required")
        return values

    @model_validator(mode="after")
    def require_actionable_clarification(self) -> Self:
        """A blocking clarification signal must identify what needs resolution."""

        if self.needs_clarification and not self.ambiguities:
            raise ValueError(
                "needs_clarification=true requires at least one ambiguity item"
            )
        return self


def determine_requirement_planning_readiness(
    analysis: RequirementAnalysis,
    *,
    analysis_revision: int,
) -> RequirementPlanningReadiness:
    """Apply the deterministic clarification policy without an LLM call."""

    if analysis.needs_clarification:
        return RequirementPlanningReadiness(
            analysis_revision=analysis_revision,
            status=RequirementPlanningReadinessStatus.BLOCKED,
            needs_clarification=True,
            blocking_ambiguities=tuple(analysis.ambiguities),
            reason_code=(
                RequirementPlanningReadinessReasonCode.UNRESOLVED_REQUIREMENT_AMBIGUITY
            ),
        )
    return RequirementPlanningReadiness(
        analysis_revision=analysis_revision,
        status=RequirementPlanningReadinessStatus.READY,
        needs_clarification=False,
        blocking_ambiguities=(),
        reason_code=None,
    )


def require_requirement_planning_ready(
    analysis: RequirementAnalysis,
    *,
    analysis_revision: int,
) -> RequirementPlanningReadiness:
    """Return readiness or reject use of a blocked revision as planning authority."""

    readiness = determine_requirement_planning_readiness(
        analysis, analysis_revision=analysis_revision
    )
    if readiness.status is RequirementPlanningReadinessStatus.BLOCKED:
        raise RequirementPlanningReadinessError(
            f"{readiness.reason_code.value}: analysis revision "
            f"{analysis_revision} requires clarification before planning."
        )
    return readiness
