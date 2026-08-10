"""Canonical requirement specification built from a human-approved analysis."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from typing import Literal
from uuid import UUID, uuid5

from pydantic import BaseModel, ConfigDict, Field

from agentic_sdlc.requirement_analysis import (
    RequirementAnalysis,
    require_requirement_planning_ready,
)


LINEAGE_NAMESPACE = UUID("a514fa1a-3dc8-5ee1-9899-04b92f98f7fd")
SpecItemKind = Literal["FR", "NFR", "CON", "AC", "RISK", "AMB"]


class RequirementSpecItem(BaseModel):
    """One exact approved statement with application-assigned identity."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    item_id: str
    lineage_id: str
    text: str


class ApprovedRequirementSpec(BaseModel):
    """Immutable canonical packaging of the approved requirement analysis."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    spec_id: str
    lineage_id: str
    version: int = Field(ge=1)
    supersedes_spec_id: str | None
    source_analysis_revision: int = Field(ge=0)
    created_at: str
    content_hash: str
    normalized_problem_statement: str
    requirement_type: Literal["greenfield", "brownfield", "ambiguous"]
    assumptions: tuple[str, ...]
    functional_requirements: tuple[RequirementSpecItem, ...]
    nonfunctional_requirements: tuple[RequirementSpecItem, ...]
    constraints: tuple[RequirementSpecItem, ...]
    acceptance_criteria: tuple[RequirementSpecItem, ...]
    risks: tuple[RequirementSpecItem, ...]
    ambiguities: tuple[RequirementSpecItem, ...]

    def all_items(self) -> tuple[RequirementSpecItem, ...]:
        """Return every canonical item in stable namespace order."""

        return (
            *self.functional_requirements,
            *self.nonfunctional_requirements,
            *self.constraints,
            *self.acceptance_criteria,
            *self.risks,
            *self.ambiguities,
        )

    def item_ids(self, kind: SpecItemKind) -> set[str]:
        """Return valid reference IDs for one namespace."""

        collections = {
            "FR": self.functional_requirements,
            "NFR": self.nonfunctional_requirements,
            "CON": self.constraints,
            "AC": self.acceptance_criteria,
            "RISK": self.risks,
            "AMB": self.ambiguities,
        }
        return {item.item_id for item in collections[kind]}


def build_approved_requirement_spec(
    analysis: RequirementAnalysis,
    *,
    source_analysis_revision: int,
    version: int = 1,
    supersedes_spec_id: str | None = None,
    lineage_id: str | None = None,
    created_at: str | None = None,
) -> ApprovedRequirementSpec:
    """Package approved text without semantic rewriting or another LLM call."""

    require_requirement_planning_ready(
        analysis, analysis_revision=source_analysis_revision
    )

    item_groups = {
        "functional_requirements": _items("FR", analysis.functional_requirements),
        "nonfunctional_requirements": _items(
            "NFR", analysis.nonfunctional_requirements
        ),
        "constraints": _items("CON", analysis.constraints),
        "acceptance_criteria": _items("AC", analysis.acceptance_criteria),
        "risks": _items("RISK", analysis.risks),
        "ambiguities": _items("AMB", analysis.ambiguities),
    }
    content = {
        "source_analysis_revision": source_analysis_revision,
        "normalized_problem_statement": analysis.normalized_problem_statement,
        "requirement_type": analysis.requirement_type,
        "assumptions": analysis.assumptions,
        "functional_requirements": [
            item.model_dump(mode="json") for item in item_groups["functional_requirements"]
        ],
        "nonfunctional_requirements": [
            item.model_dump(mode="json")
            for item in item_groups["nonfunctional_requirements"]
        ],
        "constraints": [
            item.model_dump(mode="json") for item in item_groups["constraints"]
        ],
        "acceptance_criteria": [
            item.model_dump(mode="json")
            for item in item_groups["acceptance_criteria"]
        ],
        "risks": [item.model_dump(mode="json") for item in item_groups["risks"]],
        "ambiguities": [
            item.model_dump(mode="json") for item in item_groups["ambiguities"]
        ],
    }
    content_hash = _content_hash(content)
    spec_lineage_id = lineage_id or str(
        uuid5(LINEAGE_NAMESPACE, f"approved-requirement-spec:{content_hash}")
    )
    return ApprovedRequirementSpec(
        spec_id=f"SPEC-{content_hash[:12].upper()}-V{version:03d}",
        lineage_id=spec_lineage_id,
        version=version,
        supersedes_spec_id=supersedes_spec_id,
        source_analysis_revision=source_analysis_revision,
        created_at=created_at or datetime.now(UTC).isoformat(),
        content_hash=content_hash,
        normalized_problem_statement=analysis.normalized_problem_statement,
        requirement_type=analysis.requirement_type,
        assumptions=tuple(analysis.assumptions),
        **item_groups,
    )


def _items(kind: SpecItemKind, texts: list[str]) -> tuple[RequirementSpecItem, ...]:
    items: list[RequirementSpecItem] = []
    for index, text in enumerate(texts, start=1):
        item_id = f"{kind}-{index:03d}"
        items.append(
            RequirementSpecItem(
                item_id=item_id,
                lineage_id=str(
                    uuid5(
                        LINEAGE_NAMESPACE,
                        f"requirement-item:{kind}:{item_id}:{text}",
                    )
                ),
                text=text,
            )
        )
    return tuple(items)


def _content_hash(value: object) -> str:
    canonical_json = json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )
    return hashlib.sha256(canonical_json.encode("utf-8")).hexdigest()
