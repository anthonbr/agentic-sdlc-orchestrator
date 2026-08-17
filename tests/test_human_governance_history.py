"""Lifecycle, report, manifest, and authority-boundary governance tests."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from agentic_sdlc.application import (
    GovernedRunApplicationStatus,
    GovernedRunRequest,
)
from agentic_sdlc.clarification_draft import (
    ClarificationDraftRequest,
    ClarificationDraftResult,
    FakeClarificationDrafter,
    clarification_draft_context_identity,
)
from agentic_sdlc.human_governance_history import (
    HUMAN_GOVERNANCE_HISTORY_FILENAME,
    render_human_governance_history,
)
from agentic_sdlc.llm import FakeRequirementAnalysisClient, FakeTaskPlanningClient
from agentic_sdlc.requirement_analysis import (
    RequirementAnalysis,
    RequirementPlanningReadiness,
)
from agentic_sdlc.run_events import (
    RunEventActor,
    RunEventAuthority,
    RunEventDraft,
    RunEventError,
    RunEventLog,
    RunEventType,
)
from agentic_sdlc.state import demo_input
from agentic_sdlc.streamlit_runtime import ClarificationDraftBackgroundRuntime
from tests.test_application import _service
from tests.test_streamlit_runtime import QueuedExecutor
from tests.test_workflow import (
    _analysis,
    _blocked_analysis,
    _clarified_analysis,
    _proposal,
    _proposal_without_ambiguity,
)


def _clarification_request(snapshot) -> ClarificationDraftRequest:
    gate = snapshot.human_gate
    assert gate is not None
    submission = snapshot.workflow_state["requirement_submission"]
    return ClarificationDraftRequest(
        run_id=snapshot.run_id,
        gate_token=gate.gate_token,
        analysis_revision=snapshot.workflow_state[
            "requirement_analysis_revision_count"
        ],
        original_requirement=submission["original_text"],
        requirement_analysis=RequirementAnalysis.model_validate(
            snapshot.workflow_state["requirement_analysis"], strict=False
        ),
        planning_readiness=RequirementPlanningReadiness.model_validate(
            snapshot.workflow_state["requirement_planning_readiness"],
            strict=False,
        ),
    )


def test_governance_events_report_manifest_and_publication_preserve_authority(
    tmp_path: Path,
) -> None:
    feedback = "Authentication is out of scope.\nKeep the first release local."
    service, _, _ = _service(
        tmp_path,
        analyst=FakeRequirementAnalysisClient(
            [_blocked_analysis("blocked"), _clarified_analysis("clarified")]
        ),
        planner=FakeTaskPlanningClient([_proposal_without_ambiguity()]),
        run_suffix="governance-history",
    )
    blocked = service.start_run(
        GovernedRunRequest(command="demo", workflow_input=demo_input())
    )
    event_log = RunEventLog(blocked.artifact_bundle)

    assert [event.event_type for event in event_log.read()] == [
        RunEventType.REQUIREMENT_SUBMISSION_ACCEPTED
    ]
    service.inspect_run(blocked.run_id)
    assert len(event_log.read()) == 1

    request = _clarification_request(blocked)
    context = clarification_draft_context_identity(request)
    executor = QueuedExecutor()
    drafting = ClarificationDraftBackgroundRuntime(
        executor=executor,
        event_recorder=service,
    )
    drafter = FakeClarificationDrafter(
        [
            ClarificationDraftResult(
                suggested_clarification=(
                    "Authentication is out of scope. Keep the first release local."
                )
            )
        ]
    )
    authoritative_before = blocked.workflow_state
    assert drafting.schedule("generation-1", context, request, drafter)
    assert [event.event_type for event in event_log.read()][-1] is (
        RunEventType.CLARIFICATION_DRAFT_REQUESTED
    )
    executor.run_next()
    completed = drafting.poll(context)
    assert completed.result is not None
    assert service.inspect_run(blocked.run_id).workflow_state == authoritative_before

    revised = service.resume_run(
        blocked.run_id,
        {"decision": "REQUEST_CHANGES", "feedback": feedback},
        gate_token=blocked.human_gate.gate_token,  # type: ignore[union-attr]
    )
    graph_review = service.resume_run(
        revised.run_id,
        {"decision": "APPROVE", "feedback": ""},
        gate_token=revised.human_gate.gate_token,  # type: ignore[union-attr]
    )
    terminal = service.resume_run(
        graph_review.run_id,
        {"decision": "APPROVE", "feedback": ""},
        gate_token=graph_review.human_gate.gate_token,  # type: ignore[union-attr]
    )

    events = event_log.read()
    assert [event.event_type for event in events] == [
        RunEventType.REQUIREMENT_SUBMISSION_ACCEPTED,
        RunEventType.CLARIFICATION_DRAFT_REQUESTED,
        RunEventType.CLARIFICATION_DRAFT_GENERATED,
        RunEventType.REQUIREMENT_ANALYSIS_REVIEW_DECIDED,
        RunEventType.REQUIREMENT_ANALYSIS_REVIEW_DECIDED,
        RunEventType.TASK_GRAPH_REVIEW_DECIDED,
    ]
    request_event, generated_event = events[1:3]
    assert request_event.actor is RunEventActor.HUMAN
    assert generated_event.actor is RunEventActor.AI_ASSISTANT
    assert request_event.authority is RunEventAuthority.NON_AUTHORITATIVE_ASSISTANCE
    assert generated_event.authority is (
        RunEventAuthority.NON_AUTHORITATIVE_ASSISTANCE
    )
    review_events = events[3:]
    assert all(
        event.authority is RunEventAuthority.HUMAN_GOVERNANCE
        for event in review_events
    )
    assert review_events[0].data["decision"] == "REQUEST_CHANGES"
    assert review_events[0].data["feedback_sha256"] == hashlib.sha256(
        feedback.encode("utf-8")
    ).hexdigest()
    assert feedback not in terminal.artifact_bundle.run_events_path.read_text()
    assert "suggested_clarification" not in (
        terminal.artifact_bundle.run_events_path.read_text()
    )

    report_path = (
        terminal.artifact_bundle.artifact_dir
        / HUMAN_GOVERNANCE_HISTORY_FILENAME
    )
    report = report_path.read_text(encoding="utf-8")
    assert report == render_human_governance_history(
        events,
        terminal.workflow_state,
    )
    assert "derived and non-authoritative" in report
    assert feedback in report
    assert "No workflow governance decision was made" in report
    assert "did not approve, request changes, create a revision" in report
    assert "became the authoritative approved requirement specification" in report
    assert "became the approved TaskGraph authorized for governed execution" in report

    manifest = json.loads(terminal.manifest_path.read_text(encoding="utf-8"))
    records = {record["path"]: record for record in manifest["files"]}
    assert HUMAN_GOVERNANCE_HISTORY_FILENAME in records
    assert "run-events.jsonl" not in records
    report_bytes = report_path.read_bytes()
    assert records[HUMAN_GOVERNANCE_HISTORY_FILENAME]["sha256"] == (
        hashlib.sha256(report_bytes).hexdigest()
    )
    assert terminal.export_result is not None
    assert terminal.export_result.destination_directory is not None
    published = (
        terminal.export_result.destination_directory
        / "sdlc-artifacts"
        / HUMAN_GOVERNANCE_HISTORY_FILENAME
    )
    assert published.read_bytes() == report_bytes
    service.inspect_run(terminal.run_id)
    assert event_log.read() == events


def test_rejection_is_reported_as_human_governance_and_safe_stop(
    tmp_path: Path,
) -> None:
    service, _, _ = _service(
        tmp_path,
        analyst=FakeRequirementAnalysisClient([_analysis()]),
        planner=FakeTaskPlanningClient([_proposal()]),
        run_suffix="governance-reject",
    )
    paused = service.start_run(
        GovernedRunRequest(command="demo", workflow_input=demo_input())
    )
    stopped = service.resume_run(
        paused.run_id,
        {"decision": "REJECT", "feedback": "Not approved for this release."},
        gate_token=paused.human_gate.gate_token,  # type: ignore[union-attr]
    )

    events = RunEventLog(stopped.artifact_bundle).read()
    assert events[-1].event_type is (
        RunEventType.REQUIREMENT_ANALYSIS_REVIEW_DECIDED
    )
    assert events[-1].data["decision"] == "REJECT"
    report = (
        stopped.artifact_bundle.artifact_dir
        / HUMAN_GOVERNANCE_HISTORY_FILENAME
    ).read_text(encoding="utf-8")
    assert "**Human decision:** `REJECT`" in report
    assert "safely stopped" in report
    assert stopped.export_result is None


def test_stale_clarification_context_is_not_scheduled_or_recorded(
    tmp_path: Path,
) -> None:
    service, _, _ = _service(
        tmp_path,
        analyst=FakeRequirementAnalysisClient(
            [_blocked_analysis("blocked"), _clarified_analysis("clarified")]
        ),
        planner=FakeTaskPlanningClient([_proposal_without_ambiguity()]),
        run_suffix="stale-clarification-audit",
    )
    blocked = service.start_run(
        GovernedRunRequest(command="demo", workflow_input=demo_input())
    )
    stale_request = _clarification_request(blocked)
    stale_context = clarification_draft_context_identity(stale_request)
    revised = service.resume_run(
        blocked.run_id,
        {"decision": "REQUEST_CHANGES", "feedback": "Clarify the scope."},
        gate_token=blocked.human_gate.gate_token,  # type: ignore[union-attr]
    )
    before = RunEventLog(revised.artifact_bundle).read()
    executor = QueuedExecutor()
    drafting = ClarificationDraftBackgroundRuntime(
        executor=executor,
        event_recorder=service,
    )
    drafter = FakeClarificationDrafter(
        [ClarificationDraftResult(suggested_clarification="Stale draft.")]
    )

    assert not drafting.schedule(
        "stale-generation",
        stale_context,
        stale_request,
        drafter,
    )
    assert not executor.jobs
    assert drafter.calls == []
    assert RunEventLog(revised.artifact_bundle).read() == before


class _FailOnceReviewLog(RunEventLog):
    fail_review_once = False

    def append(self, draft: RunEventDraft):
        if (
            self.fail_review_once
            and draft.event_type
            is RunEventType.REQUIREMENT_ANALYSIS_REVIEW_DECIDED
        ):
            self.fail_review_once = False
            raise RunEventError("injected audit append failure")
        return super().append(draft)


class _ControlledTerminalReviewLog(RunEventLog):
    failing_event_type: RunEventType | None = None
    failures_remaining = 0
    fail_persistently = False
    matching_attempts = 0

    def append(self, draft: RunEventDraft):
        if draft.event_type is self.failing_event_type:
            self.matching_attempts += 1
            if self.fail_persistently or self.failures_remaining > 0:
                self.failures_remaining = max(0, self.failures_remaining - 1)
                raise RunEventError("injected terminal audit append failure")
        return super().append(draft)


def test_logging_failure_cannot_undo_approval_and_inspection_repairs_event(
    tmp_path: Path,
) -> None:
    logs: list[_FailOnceReviewLog] = []

    def log_factory(bundle):
        log = _FailOnceReviewLog(bundle)
        logs.append(log)
        return log

    service, _, _ = _service(
        tmp_path,
        analyst=FakeRequirementAnalysisClient([_analysis()]),
        planner=FakeTaskPlanningClient([_proposal()]),
        run_suffix="audit-repair",
        run_event_log_factory=log_factory,
    )
    paused = service.start_run(
        GovernedRunRequest(command="demo", workflow_input=demo_input())
    )
    logs[0].fail_review_once = True

    graph_gate = service.resume_run(
        paused.run_id,
        {"decision": "APPROVE", "feedback": ""},
        gate_token=paused.human_gate.gate_token,  # type: ignore[union-attr]
    )

    assert graph_gate.workflow_state["requirement_review_decision"] == "APPROVE"
    assert graph_gate.workflow_state["approved_requirement_spec"]
    assert any("run-event append" in warning for warning in graph_gate.warnings)
    assert [event.event_type for event in logs[0].read()] == [
        RunEventType.REQUIREMENT_SUBMISSION_ACCEPTED
    ]

    inspected = service.inspect_run(paused.run_id)
    assert inspected.workflow_state["requirement_review_decision"] == "APPROVE"
    assert [event.event_type for event in logs[0].read()] == [
        RunEventType.REQUIREMENT_SUBMISSION_ACCEPTED,
        RunEventType.REQUIREMENT_ANALYSIS_REVIEW_DECIDED,
    ]
    service.inspect_run(paused.run_id)
    assert len(logs[0].read()) == 2


def test_terminal_reconciliation_repairs_transient_task_graph_audit_failure(
    tmp_path: Path,
) -> None:
    logs: list[_ControlledTerminalReviewLog] = []

    def log_factory(bundle):
        log = _ControlledTerminalReviewLog(bundle)
        logs.append(log)
        return log

    service, _, _ = _service(
        tmp_path,
        analyst=FakeRequirementAnalysisClient([_analysis()]),
        planner=FakeTaskPlanningClient([_proposal()]),
        run_suffix="terminal-audit-repair",
        run_event_log_factory=log_factory,
    )
    requirement_gate = service.start_run(
        GovernedRunRequest(command="demo", workflow_input=demo_input())
    )
    graph_gate = service.resume_run(
        requirement_gate.run_id,
        {"decision": "APPROVE", "feedback": ""},
        gate_token=requirement_gate.human_gate.gate_token,  # type: ignore[union-attr]
    )
    logs[0].failing_event_type = RunEventType.TASK_GRAPH_REVIEW_DECIDED
    logs[0].failures_remaining = 1

    terminal = service.resume_run(
        graph_gate.run_id,
        {"decision": "APPROVE", "feedback": ""},
        gate_token=graph_gate.human_gate.gate_token,  # type: ignore[union-attr]
    )

    assert terminal.workflow_status == "success"
    assert terminal.application_status is GovernedRunApplicationStatus.SUCCEEDED
    assert terminal.workflow_state["task_graph_review_history"][-1]["decision"] == (
        "APPROVE"
    )
    assert terminal.workflow_state["approved_task_graph"]
    assert logs[0].matching_attempts == 2
    assert any("run-event append" in warning for warning in terminal.warnings)
    task_graph_events = tuple(
        event
        for event in logs[0].read()
        if event.event_type is RunEventType.TASK_GRAPH_REVIEW_DECIDED
    )
    assert len(task_graph_events) == 1
    assert task_graph_events[0].actor is RunEventActor.HUMAN
    assert task_graph_events[0].authority is RunEventAuthority.HUMAN_GOVERNANCE
    assert task_graph_events[0].data["decision"] == "APPROVE"

    report_path = (
        terminal.artifact_bundle.artifact_dir
        / HUMAN_GOVERNANCE_HISTORY_FILENAME
    )
    report_bytes = report_path.read_bytes()
    assert b"became the approved TaskGraph authorized" in report_bytes
    assert terminal.manifest_path is not None
    manifest = json.loads(terminal.manifest_path.read_text(encoding="utf-8"))
    assert HUMAN_GOVERNANCE_HISTORY_FILENAME in {
        record["path"] for record in manifest["files"]
    }
    assert terminal.export_result is not None
    assert terminal.export_result.destination_directory is not None
    published_report = (
        terminal.export_result.destination_directory
        / "sdlc-artifacts"
        / HUMAN_GOVERNANCE_HISTORY_FILENAME
    )
    assert published_report.read_bytes() == report_bytes

    service.inspect_run(terminal.run_id)
    assert len(
        [
            event
            for event in logs[0].read()
            if event.event_type is RunEventType.TASK_GRAPH_REVIEW_DECIDED
        ]
    ) == 1


def test_persistent_terminal_task_graph_audit_failure_blocks_evidence_freeze(
    tmp_path: Path,
) -> None:
    logs: list[_ControlledTerminalReviewLog] = []

    def log_factory(bundle):
        log = _ControlledTerminalReviewLog(bundle)
        logs.append(log)
        return log

    service, _, _ = _service(
        tmp_path,
        analyst=FakeRequirementAnalysisClient([_analysis()]),
        planner=FakeTaskPlanningClient([_proposal()]),
        run_suffix="terminal-audit-persistent",
        run_event_log_factory=log_factory,
    )
    requirement_gate = service.start_run(
        GovernedRunRequest(command="demo", workflow_input=demo_input())
    )
    graph_gate = service.resume_run(
        requirement_gate.run_id,
        {"decision": "APPROVE", "feedback": ""},
        gate_token=requirement_gate.human_gate.gate_token,  # type: ignore[union-attr]
    )
    logs[0].failing_event_type = RunEventType.TASK_GRAPH_REVIEW_DECIDED
    logs[0].fail_persistently = True

    terminal = service.resume_run(
        graph_gate.run_id,
        {"decision": "APPROVE", "feedback": ""},
        gate_token=graph_gate.human_gate.gate_token,  # type: ignore[union-attr]
    )

    assert terminal.workflow_status == "success"
    assert terminal.application_status is GovernedRunApplicationStatus.FAILED
    assert terminal.workflow_state["task_graph_review_history"][-1]["decision"] == (
        "APPROVE"
    )
    assert terminal.workflow_state["approved_task_graph"]
    assert len(terminal.workflow_state["task_graph_review_history"]) == 1
    assert terminal.human_gate is None
    assert terminal.application_error is not None
    assert "Terminal evidence finalization failed" in terminal.application_error
    assert "not completely retained" in terminal.application_error
    assert any("run-event append" in warning for warning in terminal.warnings)
    assert logs[0].matching_attempts == 2
    assert all(
        event.event_type is not RunEventType.TASK_GRAPH_REVIEW_DECIDED
        for event in logs[0].read()
    )
    assert terminal.manifest_path is None
    assert terminal.export_result is None
    assert not (
        terminal.artifact_bundle.artifact_dir
        / HUMAN_GOVERNANCE_HISTORY_FILENAME
    ).exists()
    assert not (terminal.artifact_bundle.artifact_dir / "manifest.json").exists()
    assert not (tmp_path / "projects").exists()


def test_persistent_terminal_rejection_audit_failure_preserves_safe_stop_authority(
    tmp_path: Path,
) -> None:
    logs: list[_ControlledTerminalReviewLog] = []

    def log_factory(bundle):
        log = _ControlledTerminalReviewLog(bundle)
        logs.append(log)
        return log

    service, _, _ = _service(
        tmp_path,
        analyst=FakeRequirementAnalysisClient([_analysis()]),
        planner=FakeTaskPlanningClient([_proposal()]),
        run_suffix="rejection-audit-persistent",
        run_event_log_factory=log_factory,
    )
    requirement_gate = service.start_run(
        GovernedRunRequest(command="demo", workflow_input=demo_input())
    )
    logs[0].failing_event_type = (
        RunEventType.REQUIREMENT_ANALYSIS_REVIEW_DECIDED
    )
    logs[0].fail_persistently = True

    stopped = service.resume_run(
        requirement_gate.run_id,
        {"decision": "REJECT", "feedback": "Not approved."},
        gate_token=requirement_gate.human_gate.gate_token,  # type: ignore[union-attr]
    )

    assert stopped.workflow_status == "safe_stopped"
    assert stopped.application_status is GovernedRunApplicationStatus.FAILED
    assert stopped.workflow_state["requirement_review_decision"] == "REJECT"
    assert stopped.workflow_state["safe_stop_reason"]
    assert len(stopped.workflow_state["requirement_review_history"]) == 1
    assert stopped.human_gate is None
    assert stopped.application_error is not None
    assert "Terminal evidence finalization failed" in stopped.application_error
    assert any("run-event append" in warning for warning in stopped.warnings)
    assert logs[0].matching_attempts == 2
    assert all(
        event.event_type is not RunEventType.REQUIREMENT_ANALYSIS_REVIEW_DECIDED
        for event in logs[0].read()
    )
    assert stopped.manifest_path is None
    assert stopped.export_result is None
    assert not (
        stopped.artifact_bundle.artifact_dir
        / HUMAN_GOVERNANCE_HISTORY_FILENAME
    ).exists()
    assert not (stopped.artifact_bundle.artifact_dir / "manifest.json").exists()


def test_authority_modules_do_not_read_event_or_markdown_reports() -> None:
    repository_root = Path(__file__).parents[1]
    for relative_path in (
        "src/agentic_sdlc/nodes.py",
        "src/agentic_sdlc/workflow.py",
        "src/agentic_sdlc/validation_execution.py",
        "src/agentic_sdlc/project_export.py",
    ):
        source = (repository_root / relative_path).read_text(encoding="utf-8")
        assert "run-events.jsonl" not in source
        assert "human_governance_history.md" not in source
        assert "human_governance_history" not in source
    report_source = (
        repository_root
        / "src/agentic_sdlc/human_governance_history.py"
    ).read_text(encoding="utf-8")
    assert ".read_text(" not in report_source
