"""Deterministic human-readable report over semantic events and governed evidence."""

from __future__ import annotations

import hashlib
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import cast

from agentic_sdlc.run_events import (
    RunEvent,
    RunEventActor,
    RunEventAuthority,
    RunEventLog,
    RunEventType,
)


HUMAN_GOVERNANCE_HISTORY_FILENAME = "human_governance_history.md"
_DISCLAIMER = (
    "This report is derived and non-authoritative. Existing governed workflow "
    "state and retained evidence remain authoritative."
)


class HumanGovernanceHistoryError(ValueError):
    """The events and authoritative evidence cannot support one honest report."""


def write_human_governance_history(
    state: Mapping[str, object],
    event_log: RunEventLog,
    output_dir: Path,
) -> Path:
    """Validate the live event stream and atomically install its derived report."""

    events = event_log.read()
    report = render_human_governance_history(events, state)
    path = output_dir / HUMAN_GOVERNANCE_HISTORY_FILENAME
    _atomic_write_text(path, report)
    return path


def render_human_governance_history(
    events: Sequence[RunEvent],
    state: Mapping[str, object],
) -> str:
    """Render canonical event sequence without strengthening recorded evidence."""

    run_id = _required_text(state.get("run_id"), "run ID")
    _validate_event_chronology(events, run_id=run_id)
    lines = [
        "# Human Governance History",
        "",
        _DISCLAIMER,
        "",
        "This report explains observed human input, human governance, "
        "non-authoritative AI assistance, and automated consequences. It is never "
        "read to approve, resume, revise, validate, mutate, or publish a run.",
        "",
        "## Run",
        "",
        f"- Run ID: `{_code(run_id)}`",
        "- Workflow mode: "
        + ("`BROWNFIELD`" if state.get("brownfield_baseline") else "`GREENFIELD`"),
        f"- Final status: `{_code(str(state.get('workflow_status', 'unknown')))}`",
        "- Canonical chronology: semantic event `sequence` (timestamps are audit "
        "metadata only).",
        "",
    ]
    requested_clarifications: set[tuple[str, str]] = set()
    for event in events:
        if event.event_type is RunEventType.REQUIREMENT_SUBMISSION_ACCEPTED:
            _append_requirement_submission(lines, event, state)
        elif event.event_type is RunEventType.BROWNFIELD_BASELINE_SELECTED:
            _append_baseline_selection(lines, event, state)
        elif event.event_type is RunEventType.BROWNFIELD_BASELINE_VERIFIED:
            _append_baseline_verification(lines, event, state)
        elif event.event_type is RunEventType.REQUIREMENT_ANALYSIS_REVIEW_DECIDED:
            _append_requirement_review(lines, event, state)
        elif event.event_type is RunEventType.TASK_GRAPH_REVIEW_DECIDED:
            _append_task_graph_review(lines, event, state)
        elif event.event_type is RunEventType.CLARIFICATION_DRAFT_REQUESTED:
            key = _clarification_key(event)
            requested_clarifications.add(key)
            _append_clarification_request(lines, event, state)
        elif event.event_type is RunEventType.CLARIFICATION_DRAFT_GENERATED:
            key = _clarification_key(event)
            if key not in requested_clarifications:
                raise HumanGovernanceHistoryError(
                    "Clarification generation has no preceding request event."
                )
            _append_clarification_generation(lines, event, state)

    lines.extend(
        [
            "## Known Scope Limits",
            "",
            "- This first semantic-event slice records requirement intake, "
            "brownfield baseline choice/verification, human review decisions, and "
            "clarification assistance only.",
            "- Task execution, retry, validation, rollback, publication, latency, "
            "and reliability event families are intentionally not emitted yet.",
            "- Brownfield impact analysis is reviewed as part of Requirement "
            "Analysis; this report does not claim a separate impact-approval gate.",
            "- Missing evidence remains missing and is never inferred from prose, "
            "names, files, or timestamps.",
            "",
        ]
    )
    return "\n".join(lines)


def _append_requirement_submission(
    lines: list[str],
    event: RunEvent,
    state: Mapping[str, object],
) -> None:
    submission = _required_mapping(
        state.get("requirement_submission"), "requirement submission"
    )
    for field in ("original_sha256", "normalized_sha256", "source_kind"):
        if event.data[field] != submission.get(field):
            raise HumanGovernanceHistoryError(
                "Requirement submission event differs from authoritative intake."
            )
    original_text = _required_text(
        submission.get("original_text"),
        "original submitted requirement",
    )
    analysis = _first_revision(state, "requirement_analysis_history")
    result = (
        "Requirement Analysis revision "
        f"{analysis['revision_number']} entered governed analysis."
        if analysis is not None
        else "No validated Requirement Analysis revision is established."
    )
    _start_event_section(lines, event, "Requirement Submission")
    lines.extend(
        [
            "**Action:** Requirement submitted and accepted into the governed run.",
            "",
            f"**Source:** `{_code(str(submission['source_kind']))}`",
            "",
            "**Original submitted requirement:**",
            "",
            *_fenced_text_lines(original_text),
            "",
            "**Original requirement SHA-256:** "
            f"`{_code(str(event.data['original_sha256']))}`",
            "",
            "**Normalized requirement SHA-256:** "
            f"`{_code(str(event.data['normalized_sha256']))}`",
            "",
            f"**Result:** {result}",
            "",
        ]
    )


def _append_baseline_selection(
    lines: list[str],
    event: RunEvent,
    state: Mapping[str, object],
) -> None:
    baseline = _matching_baseline(event, state)
    _start_event_section(lines, event, "Brownfield Baseline Selected")
    lines.extend(
        [
            "**Action:** Selected "
            f"`{_code(str(baseline['selected_project_name']))}` as the brownfield "
            "baseline.",
            "",
            f"**Originating run:** `{_code(str(baseline['originating_run_id']))}`",
            "",
            f"**Source snapshot:** `{_code(str(baseline['source_snapshot_id']))}`",
            "",
            "**Result:** The human baseline choice was recorded as input. "
            "Automated identity and integrity verification is reported separately.",
            "",
        ]
    )


def _append_baseline_verification(
    lines: list[str],
    event: RunEvent,
    state: Mapping[str, object],
) -> None:
    baseline = _matching_baseline(event, state)
    context = _required_mapping(
        state.get("brownfield_codebase_context"), "brownfield codebase context"
    )
    if (
        event.data["verified"] is not True
        or event.data["codebase_context_id"] != context.get("context_id")
        or context.get("baseline_id") != baseline.get("baseline_id")
        or event.data["governed_baseline_snapshot_id"]
        != baseline.get("governed_baseline_snapshot_id")
    ):
        raise HumanGovernanceHistoryError(
            "Brownfield verification event is not supported by retained evidence."
        )
    _start_event_section(lines, event, "Brownfield Baseline Verified")
    lines.extend(
        [
            "**Automated verification:** `PASSED`",
            "",
            f"**Baseline ID:** `{_code(str(baseline['baseline_id']))}`",
            "",
            "**Governed baseline snapshot:** "
            f"`{_code(str(baseline['governed_baseline_snapshot_id']))}`",
            "",
            f"**Codebase context:** `{_code(str(context['context_id']))}`",
            "",
            "**Result:** Governed brownfield analysis began from the verified "
            "published baseline and bounded codebase context.",
            "",
        ]
    )


def _append_requirement_review(
    lines: list[str],
    event: RunEvent,
    state: Mapping[str, object],
) -> None:
    review, revision_record = _authoritative_review(
        event,
        state,
        review_history_name="requirement_review_history",
        revision_history_name="requirement_analysis_history",
    )
    revision = int(review["revision_number"])
    analysis = _required_mapping(revision_record.get("analysis"), "analysis revision")
    readiness = _required_mapping(
        revision_record.get("planning_readiness"), "planning readiness"
    )
    _start_event_section(
        lines,
        event,
        f"Requirement Analysis Review — Revision {revision}",
    )
    lines.extend(
        [
            f"**Planning readiness:** `{_code(str(readiness.get('status', 'unknown')))}`",
            "",
            f"**Human decision:** `{_code(str(review['decision']))}`",
            "",
            "**Human feedback:**",
            "",
            *_feedback_lines(str(review["feedback"])),
            "",
            f"**Result:** {_requirement_review_result(review, state)}",
            "",
        ]
    )
    if analysis.get("brownfield_impact") is not None:
        lines.extend(
            [
                "**Brownfield impact governance:** The impact analysis was reviewed "
                "as part of this Requirement Analysis decision; no independent "
                "impact-approval gate is claimed.",
                "",
            ]
        )


def _append_task_graph_review(
    lines: list[str],
    event: RunEvent,
    state: Mapping[str, object],
) -> None:
    review, revision_record = _authoritative_review(
        event,
        state,
        review_history_name="task_graph_review_history",
        revision_history_name="task_graph_history",
    )
    revision = int(review["revision_number"])
    graph = _required_mapping(revision_record.get("task_graph"), "TaskGraph revision")
    _start_event_section(lines, event, f"TaskGraph Review — Revision {revision}")
    lines.extend(
        [
            f"**TaskGraph:** `{_code(str(graph.get('graph_id', 'unknown')))}`",
            "",
            f"**Human decision:** `{_code(str(review['decision']))}`",
            "",
            "**Human feedback:**",
            "",
            *_feedback_lines(str(review["feedback"])),
            "",
            f"**Result:** {_task_graph_review_result(review, graph, state)}",
            "",
        ]
    )


def _append_clarification_request(
    lines: list[str],
    event: RunEvent,
    state: Mapping[str, object],
) -> None:
    _require_analysis_revision(event, state)
    _start_event_section(lines, event, "AI Clarification Assistance Requested")
    lines.extend(
        [
            "**Human action:** Requested AI clarification drafting assistance.",
            "",
            "**Authority:** `NON_AUTHORITATIVE_ASSISTANCE`",
            "",
            "**Result:** Drafting was requested for human review. No workflow "
            "governance decision was made.",
            "",
        ]
    )


def _append_clarification_generation(
    lines: list[str],
    event: RunEvent,
    state: Mapping[str, object],
) -> None:
    _require_analysis_revision(event, state)
    _start_event_section(lines, event, "AI Clarification Draft Generated")
    lines.extend(
        [
            "**AI result:** Draft generated and made available for human review.",
            "",
            "**Authority:** `NON_AUTHORITATIVE_ASSISTANCE`",
            "",
            f"**Model:** `{_code(str(event.data['model_name']))}`",
            "",
            f"**Draft SHA-256:** `{_code(str(event.data['draft_sha256']))}`",
            "",
            f"**Character count:** {event.data['character_count']}",
            "",
            "**Result:** The AI produced editable assistance only. It did not "
            "approve, request changes, create a revision, or resume the workflow.",
            "",
        ]
    )


def _start_event_section(
    lines: list[str],
    event: RunEvent,
    title: str,
) -> None:
    lines.extend(
        [
            f"## {event.sequence:03d} — {title}",
            "",
            f"**Actor:** `{event.actor.value}`",
            "",
            f"**Authority classification:** `{event.authority.value}`",
            "",
        ]
    )


def _authoritative_review(
    event: RunEvent,
    state: Mapping[str, object],
    *,
    review_history_name: str,
    revision_history_name: str,
) -> tuple[Mapping[str, object], Mapping[str, object]]:
    sequence = event.correlation.review_sequence
    revision = (
        event.correlation.analysis_revision
        if event.event_type is RunEventType.REQUIREMENT_ANALYSIS_REVIEW_DECIDED
        else event.correlation.task_graph_revision
    )
    if sequence is None or revision is None:
        raise HumanGovernanceHistoryError("Review event correlation is incomplete.")
    reviews = _mapping_sequence(state.get(review_history_name, ()), review_history_name)
    matches = tuple(item for item in reviews if item.get("sequence") == sequence)
    if len(matches) != 1:
        raise HumanGovernanceHistoryError(
            "Review event has no exact authoritative history entry."
        )
    review = matches[0]
    feedback = review.get("feedback")
    if not isinstance(feedback, str):
        raise HumanGovernanceHistoryError("Authoritative human feedback is invalid.")
    feedback_hash = (
        hashlib.sha256(feedback.encode("utf-8")).hexdigest() if feedback else None
    )
    if (
        review.get("revision_number") != revision
        or review.get("decision") != event.data["decision"]
        or bool(feedback) != event.data["feedback_present"]
        or feedback_hash != event.data["feedback_sha256"]
    ):
        raise HumanGovernanceHistoryError(
            "Review event differs from authoritative governance history."
        )
    revisions = _mapping_sequence(
        state.get(revision_history_name, ()), revision_history_name
    )
    revision_matches = tuple(
        item for item in revisions if item.get("revision_number") == revision
    )
    if len(revision_matches) != 1:
        raise HumanGovernanceHistoryError(
            "Review event has no exact authoritative revision evidence."
        )
    return review, revision_matches[0]


def _requirement_review_result(
    review: Mapping[str, object],
    state: Mapping[str, object],
) -> str:
    decision = review["decision"]
    revision = int(cast(int, review["revision_number"]))
    if decision == "APPROVE":
        spec = _required_mapping(
            state.get("approved_requirement_spec"),
            "approved requirement specification",
        )
        if spec.get("source_analysis_revision") != revision:
            raise HumanGovernanceHistoryError(
                "Approved specification does not match the reviewed revision."
            )
        return (
            f"`{_code(str(spec['spec_id']))}` became the authoritative approved "
            "requirement specification."
        )
    if decision == "REQUEST_CHANGES":
        next_revision = _next_revision(
            state,
            "requirement_analysis_history",
            revision,
        )
        if next_revision is None:
            return "No subsequent Requirement Analysis revision is established."
        return (
            "Requirement Analysis revision "
            f"{next_revision['revision_number']} was subsequently produced."
        )
    return _safe_stop_result(state)


def _task_graph_review_result(
    review: Mapping[str, object],
    graph: Mapping[str, object],
    state: Mapping[str, object],
) -> str:
    decision = review["decision"]
    revision = int(cast(int, review["revision_number"]))
    if decision == "APPROVE":
        approved = _required_mapping(
            state.get("approved_task_graph"), "approved TaskGraph"
        )
        if approved.get("graph_id") != graph.get("graph_id"):
            raise HumanGovernanceHistoryError(
                "Approved TaskGraph does not match the reviewed revision."
            )
        return (
            f"`{_code(str(approved['graph_id']))}` became the approved TaskGraph "
            "authorized for governed execution."
        )
    if decision == "REQUEST_CHANGES":
        next_revision = _next_revision(state, "task_graph_history", revision)
        if next_revision is None:
            return "No subsequent TaskGraph revision is established."
        next_graph = _required_mapping(next_revision.get("task_graph"), "TaskGraph")
        return (
            f"TaskGraph `{_code(str(next_graph['graph_id']))}` was subsequently "
            "produced for another human review."
        )
    return _safe_stop_result(state)


def _safe_stop_result(state: Mapping[str, object]) -> str:
    if state.get("workflow_status") != "safe_stopped":
        return "No safe-stop outcome is established in retained workflow state."
    reason = state.get("safe_stop_reason")
    if isinstance(reason, str) and reason:
        return f"The run safely stopped: {_text(reason)}"
    return "The run safely stopped after the human rejection."


def _matching_baseline(
    event: RunEvent,
    state: Mapping[str, object],
) -> Mapping[str, object]:
    baseline_value = state.get("brownfield_baseline")
    if baseline_value is None:
        raise HumanGovernanceHistoryError(
            "Greenfield state cannot support a brownfield baseline event."
        )
    baseline = _required_mapping(baseline_value, "brownfield baseline")
    if (
        event.correlation.baseline_id != baseline.get("baseline_id")
        or event.data.get("baseline_id", baseline.get("baseline_id"))
        != baseline.get("baseline_id")
    ):
        raise HumanGovernanceHistoryError(
            "Brownfield event does not match retained baseline identity."
        )
    if event.event_type is RunEventType.BROWNFIELD_BASELINE_SELECTED:
        for field in (
            "selected_project_name",
            "originating_run_id",
            "publication_bundle_sha256",
            "source_snapshot_id",
        ):
            if event.data[field] != baseline.get(field):
                raise HumanGovernanceHistoryError(
                    "Brownfield selection event differs from retained provenance."
                )
    return baseline


def _require_analysis_revision(
    event: RunEvent,
    state: Mapping[str, object],
) -> Mapping[str, object]:
    revision = event.correlation.analysis_revision
    if revision is None or event.data["analysis_revision"] != revision:
        raise HumanGovernanceHistoryError(
            "Clarification event revision correlation is incomplete."
        )
    records = _mapping_sequence(
        state.get("requirement_analysis_history", ()),
        "requirement analysis history",
    )
    matches = tuple(item for item in records if item.get("revision_number") == revision)
    if len(matches) != 1:
        raise HumanGovernanceHistoryError(
            "Clarification event has no exact Requirement Analysis revision."
        )
    return matches[0]


def _clarification_key(event: RunEvent) -> tuple[str, str]:
    generation = event.correlation.generation_id
    context = event.correlation.context_identity
    if generation is None or context is None:
        raise HumanGovernanceHistoryError(
            "Clarification event identity is incomplete."
        )
    return generation, context


def _validate_event_chronology(
    events: Sequence[RunEvent],
    *,
    run_id: str,
) -> None:
    identities: set[str] = set()
    for expected_sequence, event in enumerate(events, start=1):
        if event.run_id != run_id or event.sequence != expected_sequence:
            raise HumanGovernanceHistoryError(
                "Semantic events are not one contiguous run-owned chronology."
            )
        if event.event_id in identities:
            raise HumanGovernanceHistoryError(
                "Semantic event chronology contains a duplicate identity."
            )
        identities.add(event.event_id)


def _next_revision(
    state: Mapping[str, object],
    history_name: str,
    current_revision: int,
) -> Mapping[str, object] | None:
    candidates = tuple(
        item
        for item in _mapping_sequence(state.get(history_name, ()), history_name)
        if isinstance(item.get("revision_number"), int)
        and cast(int, item["revision_number"]) > current_revision
    )
    return min(candidates, key=lambda item: cast(int, item["revision_number"])) if candidates else None


def _first_revision(
    state: Mapping[str, object],
    history_name: str,
) -> Mapping[str, object] | None:
    records = _mapping_sequence(state.get(history_name, ()), history_name)
    return records[0] if records else None


def _feedback_lines(feedback: str) -> list[str]:
    if not feedback:
        return ["None provided."]
    return _fenced_text_lines(feedback)


def _fenced_text_lines(text: str) -> list[str]:
    # Select a fence longer than any actual backtick run while retaining the
    # authoritative source bytes verbatim inside the report.
    maximum_run = 0
    current_run = 0
    for character in text:
        current_run = current_run + 1 if character == "`" else 0
        maximum_run = max(maximum_run, current_run)
    fence = "`" * max(3, maximum_run + 1)
    return [f"{fence}text", text, fence]


def _required_mapping(value: object, label: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise HumanGovernanceHistoryError(f"{label} is unavailable or invalid.")
    return cast(Mapping[str, object], value)


def _mapping_sequence(
    value: object,
    label: str,
) -> tuple[Mapping[str, object], ...]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        raise HumanGovernanceHistoryError(f"{label} is not an ordered evidence list.")
    return tuple(_required_mapping(item, label) for item in value)


def _required_text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise HumanGovernanceHistoryError(f"{label} must be nonblank text.")
    return value


def _text(value: str) -> str:
    return " ".join(value.split())


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
