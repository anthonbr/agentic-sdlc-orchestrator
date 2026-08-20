"""Deterministic persisted reports over the read-only traceability projection."""

from __future__ import annotations

import json
from collections import Counter
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict

from agentic_sdlc.traceability import (
    BrownfieldTraceabilityLineage,
    RequirementTraceabilityProjection,
    TraceabilityRow,
    TraceabilityStatus,
    build_requirement_traceability,
    traceability_row_evaluator_reason,
    traceability_status_explanation,
    traceability_status_heading,
)


REQUIREMENT_TRACEABILITY_JSON_FILENAME = "requirement_traceability.json"
REQUIREMENT_TRACEABILITY_MARKDOWN_FILENAME = "requirement_traceability.md"
REQUIREMENT_TRACEABILITY_SCHEMA_VERSION = "requirement-traceability-v1"
_AUTHORITY_STATEMENT = (
    "This deterministic report derives from authoritative governed SDLC evidence. "
    "It creates no requirement, planning, execution, validation, mutation, readiness, "
    "or publication authority."
)
_BROWNFIELD_IMPACT_LIMIT = (
    "The approved impact analysis is traceable to the overall plan, but individual "
    "impact findings are not yet traceable to specific tasks."
)
_COMMON_LIMITATIONS = (
    "Missing relationships remain missing; names, prose, filenames, and semantic "
    "similarity are not used to infer links.",
    "Application-required final-workspace validation remains run-level evidence and "
    "does not create item-specific validation links.",
    "This report is generated before publication and does not claim that a new "
    "project publication has already succeeded.",
)


class TraceabilityStatusCounts(BaseModel):
    """Deterministic summary of derived row statuses."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    verified: int
    unverified: int
    not_implemented: int


class TraceabilityRunCompletionEvidence(BaseModel):
    """Terminal authority available before project publication begins."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    workflow_status: str
    exit_gate_passed: bool
    readiness_validation_id: str | None
    readiness_passed: bool
    final_workspace_snapshot_id: str | None
    publication_claim: Literal["NOT_INCLUDED_PRE_PUBLICATION"]


class RequirementTraceabilityArtifact(BaseModel):
    """Intentional machine-readable contract for one derived traceability report."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_version: Literal["requirement-traceability-v1"]
    artifact_kind: Literal["requirement_traceability"]
    authority: Literal["DERIVED"]
    authoritative: Literal[False]
    authority_statement: str
    authoritative_sources: tuple[str, ...]
    run_id: str
    requirement_spec_id: str
    requirement_spec_version: int
    source_analysis_revision: int
    task_graph_id: str | None
    task_graph_version: int | None
    status_counts: TraceabilityStatusCounts
    rows: tuple[TraceabilityRow, ...]
    run_completion_evidence: TraceabilityRunCompletionEvidence
    brownfield_lineage: BrownfieldTraceabilityLineage | None
    limitations: tuple[str, ...]


def build_requirement_traceability_artifact(
    projection: RequirementTraceabilityProjection,
) -> RequirementTraceabilityArtifact:
    """Wrap one pre-publication projection in an explicit derived-report contract."""

    if projection.final_authority.publication_succeeded:
        raise ValueError(
            "Persisted traceability must be derived before publication succeeds."
        )
    counts = Counter(row.status for row in projection.rows)
    final = projection.final_authority
    return RequirementTraceabilityArtifact(
        schema_version=REQUIREMENT_TRACEABILITY_SCHEMA_VERSION,
        artifact_kind="requirement_traceability",
        authority="DERIVED",
        authoritative=False,
        authority_statement=_AUTHORITY_STATEMENT,
        authoritative_sources=(
            "approved requirement specification",
            "approved TaskGraph",
            "final-authority task execution and engineering artifacts",
            "validated materialization and workspace mutation evidence",
            "exact governed validation execution evidence",
            "project readiness and final authoritative workspace snapshot",
            "brownfield baseline, context, and approved impact analysis when "
            "applicable",
        ),
        run_id=projection.run_id,
        requirement_spec_id=projection.requirement_spec_id,
        requirement_spec_version=projection.requirement_spec_version,
        source_analysis_revision=projection.source_analysis_revision,
        task_graph_id=projection.task_graph_id,
        task_graph_version=projection.task_graph_version,
        status_counts=TraceabilityStatusCounts(
            verified=counts[TraceabilityStatus.VERIFIED],
            unverified=counts[TraceabilityStatus.UNVERIFIED],
            not_implemented=counts[TraceabilityStatus.NOT_IMPLEMENTED],
        ),
        rows=projection.rows,
        run_completion_evidence=TraceabilityRunCompletionEvidence(
            workflow_status=final.workflow_status,
            exit_gate_passed=final.exit_gate_passed,
            readiness_validation_id=final.readiness_validation_id,
            readiness_passed=final.readiness_passed,
            final_workspace_snapshot_id=final.final_workspace_snapshot_id,
            publication_claim="NOT_INCLUDED_PRE_PUBLICATION",
        ),
        brownfield_lineage=projection.brownfield_lineage,
        limitations=(
            (
                *_COMMON_LIMITATIONS[:2],
                _BROWNFIELD_IMPACT_LIMIT,
                *_COMMON_LIMITATIONS[2:],
            )
            if projection.brownfield_lineage is not None
            else _COMMON_LIMITATIONS
        ),
    )


def render_requirement_traceability_json(
    artifact: RequirementTraceabilityArtifact,
) -> str:
    """Serialize the intentional contract with stable ordering and no runtime clock."""

    return (
        json.dumps(
            artifact.model_dump(mode="json"),
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        + "\n"
    )


def render_requirement_traceability_markdown(
    artifact: RequirementTraceabilityArtifact,
) -> str:
    """Render a concise human-readable report while retaining technical evidence IDs."""

    counts = artifact.status_counts
    final = artifact.run_completion_evidence
    lines = [
        "# Requirement-to-Code Traceability",
        "",
        "## About this report",
        "",
        _AUTHORITY_STATEMENT,
        "",
        "It is produced before project publication. A later successful publication "
        "copies this manifest-bound report through the normal verified evidence "
        "pipeline; this report does not claim that publication has already happened.",
        "",
        "## Summary",
        "",
        f"- VERIFIED: {counts.verified}",
        f"- UNVERIFIED: {counts.unverified}",
        f"- NOT_IMPLEMENTED: {counts.not_implemented}",
        "",
        "## Traceability Status",
        "",
    ]
    for status in TraceabilityStatus:
        lines.extend(
            [
                f"- **{status.value}** — {traceability_status_explanation(status)}",
            ]
        )
    lines.extend(
        [
            "",
            "## Run Completion Evidence",
            "",
            f"- Workflow status: `{_code(final.workflow_status)}`",
            f"- Exit gate: `{'PASS' if final.exit_gate_passed else 'NOT PASSED'}`",
            "- Readiness: "
            + (
                f"`{_code(final.readiness_validation_id)}` · "
                f"`{'PASS' if final.readiness_passed else 'NOT PASSED'}`"
                if final.readiness_validation_id
                else "not established"
            ),
            "- Final authoritative workspace snapshot: "
            + (
                f"`{_code(final.final_workspace_snapshot_id)}`"
                if final.final_workspace_snapshot_id
                else "not established"
            ),
            "- Publication: not claimed; this report is generated before publication.",
            "",
            "## Requirement Traceability",
            "",
        ]
    )
    for row in artifact.rows:
        _append_row(lines, row)
    if artifact.brownfield_lineage is not None:
        _append_brownfield_lineage(lines, artifact.brownfield_lineage)
    lines.extend(["## Known Traceability Limits", ""])
    lines.extend(f"- {item}" for item in artifact.limitations)
    lines.append("")
    return "\n".join(lines)


def write_requirement_traceability_artifacts(
    state: Mapping[str, Any],
    output_dir: Path,
) -> tuple[Path, Path]:
    """Build once and atomically install both pre-publication derived reports."""

    projection = build_requirement_traceability(state)
    artifact = build_requirement_traceability_artifact(projection)
    json_text = render_requirement_traceability_json(artifact)
    markdown_text = render_requirement_traceability_markdown(artifact)
    paths = (
        output_dir / REQUIREMENT_TRACEABILITY_JSON_FILENAME,
        output_dir / REQUIREMENT_TRACEABILITY_MARKDOWN_FILENAME,
    )
    try:
        _atomic_write_text(paths[0], json_text)
        _atomic_write_text(paths[1], markdown_text)
    except OSError:
        for path in paths:
            path.unlink(missing_ok=True)
        raise
    return paths


def _append_row(lines: list[str], row: TraceabilityRow) -> None:
    authority = row.authority_links[0]
    lines.extend(
        [
            f"### {_markdown_text(row.item_id)} — {_markdown_text(row.text)}",
            "",
            f"**Traceability status:** {traceability_status_heading(row.status)}",
            "",
            f"**Spec:** `{_code(authority.spec_id)}` "
            f"V{authority.spec_version:03d} · analysis revision "
            f"{authority.source_analysis_revision}",
            "",
            "**Tasks:** " + _task_summary(row),
            "",
            "**Files changed:** " + _implementation_summary(row),
            "",
            "**Validation performed:** " + _validation_summary(row),
            "",
            "**Evidence:** " + _evidence_summary(row),
            "",
            f"**Reason:** {_markdown_text(traceability_row_evaluator_reason(row))}",
            "",
            "**Gaps:**",
            "",
        ]
    )
    if row.gaps:
        lines.extend(
            f"- `{gap.code.value}` — {_markdown_text(gap.detail)}"
            for gap in row.gaps
        )
    else:
        lines.append("- None.")
    lines.extend(["", "#### Technical evidence", ""])
    lines.append(
        f"- Authority `[{authority.basis.value}]` item lineage "
        f"`{_code(authority.item_lineage_id)}`"
    )
    lines.append(f"- Projection reason: {_markdown_text(row.status_reason)}")
    if row.task_links:
        lines.extend(
            f"- Task `[{link.basis.value}]` `{_code(link.task_id)}` — "
            f"{_markdown_text(link.title)}"
            for link in row.task_links
        )
    if row.artifact_links:
        lines.extend(
            f"- Generated artifact `[{link.basis.value}]` "
            f"`{_code(link.artifact_id)}` · type `{_code(link.artifact_type)}` · "
            f"logical name `{_code(link.logical_name)}` · task "
            f"`{_code(link.task_id)}` · request `{_code(link.request_id)}` · "
            f"attempt `{_code(link.attempt_id)}`"
            for link in row.artifact_links
        )
    if row.implementation_links:
        lines.extend(
            f"- Materialization `[{link.basis.value}]` "
            f"`{_code(link.materialization_validation_id)}` · artifact "
            f"`{_code(link.artifact_id)}` · target `{_code(link.target_path)}` · "
            f"change set "
            f"`{_code(link.change_set_id)}` · mutation `{_code(link.mutation_id)}` · "
            f"preimage `{_code(link.expected_preimage_hash or 'none')}` · postimage "
            f"`{_code(link.observed_postimage_hash)}`"
            for link in row.implementation_links
        )
    if row.validation_links:
        lines.extend(
            f"- Validation `[{link.basis.value}]` requirement "
            f"`{_code(link.validation_requirement_id)}` · evidence "
            f"`{_code(link.evidence_id)}` · policy `{_code(link.policy_id)}` "
            f"`{_code(link.policy_version)}`"
            + (
                " · provisioning "
                + ", ".join(
                    f"`{_code(item)}`" for item in link.provisioning_evidence_ids
                )
                if link.provisioning_evidence_ids
                else ""
            )
            for link in row.validation_links
        )
    if row.evidence_links:
        lines.extend(
            f"- Evidence `[{link.basis.value}]` `{_code(link.evidence_kind)}` "
            f"`{_code(link.evidence_id)}`"
            for link in row.evidence_links
        )
    if not (
        row.task_links
        or row.artifact_links
        or row.implementation_links
        or row.validation_links
        or row.evidence_links
    ):
        lines.append("- No technical relationship evidence is linked.")
    lines.append("")


def _append_brownfield_lineage(
    lines: list[str],
    lineage: BrownfieldTraceabilityLineage,
) -> None:
    lines.extend(["## Brownfield Lineage", ""])
    if lineage.verified:
        lines.extend(
            f"{index}. **{_markdown_text(step.stage)}** — "
            f"`{_code(step.identity)}` `[{step.basis.value}]`  \n"
            f"   {_markdown_text(step.detail)}"
            for index, step in enumerate(lineage.steps, start=1)
        )
    else:
        lines.append(
            "The available records do not establish one exact correlated "
            "brownfield lineage."
        )
        lines.extend(
            f"- `{gap.code.value}` — {_markdown_text(gap.detail)}"
            for gap in lineage.gaps
        )
    lines.extend(
        [
            "",
            _BROWNFIELD_IMPACT_LIMIT,
            "",
            "Missing links are shown explicitly rather than inferred.",
            "",
        ]
    )


def _task_summary(row: TraceabilityRow) -> str:
    if not row.task_links:
        return "No approved TaskGraph task explicitly references this item."
    return "; ".join(
        f"`{_code(link.task_id)}` — {_markdown_text(link.title)}"
        for link in row.task_links
    )


def _implementation_summary(row: TraceabilityRow) -> str:
    if not row.implementation_links:
        return "No authoritative materialized implementation target is linked."
    return "; ".join(
        f"`{_code(link.target_path)}` — `{link.operation.value}`"
        for link in row.implementation_links
    )


def _validation_summary(row: TraceabilityRow) -> str:
    if not row.validation_links:
        return "No qualifying governed validation is explicitly linked."
    return "; ".join(
        f"`{link.profile.value}` — `PASS` through `{_code(link.task_id)}`"
        for link in row.validation_links
    )


def _evidence_summary(row: TraceabilityRow) -> str:
    parts = []
    if row.artifact_links:
        parts.append(f"{len(row.artifact_links)} generated artifact record(s)")
    mutation_count = sum(
        link.evidence_kind == "WORKSPACE_MUTATION" for link in row.evidence_links
    )
    if mutation_count:
        parts.append(f"{mutation_count} mutation record(s)")
    if row.validation_links:
        parts.append(f"{len(row.validation_links)} validation record(s)")
    return "; ".join(parts) if parts else "No final evidence relationship is linked."


def _markdown_text(value: str) -> str:
    compact = " ".join(value.split())
    for character in ("\\", "`", "*", "_", "[", "]", "<", ">", "#", "|"):
        compact = compact.replace(character, f"\\{character}")
    return compact


def _code(value: str) -> str:
    return " ".join(value.split()).replace("`", "\\`")


def _atomic_write_text(path: Path, contents: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    try:
        temporary.write_text(contents, encoding="utf-8")
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)
