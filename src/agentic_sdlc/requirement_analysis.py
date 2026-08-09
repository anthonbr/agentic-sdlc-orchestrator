"""Validated structured output for governed requirement analysis."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


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
