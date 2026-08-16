"""Append-only semantic observations of one governed run.

The event stream is operational audit evidence.  It never reconstructs workflow
state and never grants governance or execution authority.
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from threading import Lock
from typing import Literal, TypeAlias, cast

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    field_serializer,
    field_validator,
    model_validator,
)

from agentic_sdlc.brownfield_baseline import BrownfieldBaselineProvenance
from agentic_sdlc.brownfield_context import BrownfieldCodebaseContext
from agentic_sdlc.clarification_draft import (
    ClarificationDraftRequest,
    ClarificationDraftResult,
    clarification_draft_context_identity,
)
from agentic_sdlc.run_artifacts import (
    RUNS_DIRECTORY_NAME,
    RUN_EVENTS_FILENAME,
    LiveRunArtifactBundle,
)


RUN_EVENT_SCHEMA_VERSION = "run-event-v1"
_EVENT_ID_PREFIX = "RUN-EVENT-"
_SHA256_LENGTH = 64


class RunEventError(RuntimeError):
    """Base failure for operational semantic run-event handling."""


class RunEventIntegrityError(RunEventError):
    """The persisted stream or supplied semantic evidence is inconsistent."""


class RunEventConflictError(RunEventIntegrityError):
    """A stable semantic identity was reused with different event content."""


class RunEventActor(StrEnum):
    """Who performed the observed semantic action."""

    HUMAN = "HUMAN"
    AI_ASSISTANT = "AI_ASSISTANT"
    SYSTEM = "SYSTEM"


class RunEventAuthority(StrEnum):
    """Authority classification kept separate from the actor identity."""

    HUMAN_INPUT = "HUMAN_INPUT"
    HUMAN_GOVERNANCE = "HUMAN_GOVERNANCE"
    NON_AUTHORITATIVE_ASSISTANCE = "NON_AUTHORITATIVE_ASSISTANCE"
    AUTOMATED_CONSEQUENCE = "AUTOMATED_CONSEQUENCE"


class RunEventStage(StrEnum):
    """Small initial vocabulary of governed lifecycle areas."""

    REQUIREMENT_SUBMISSION = "REQUIREMENT_SUBMISSION"
    BROWNFIELD_BASELINE = "BROWNFIELD_BASELINE"
    REQUIREMENT_ANALYSIS = "REQUIREMENT_ANALYSIS"
    TASK_GRAPH = "TASK_GRAPH"
    CLARIFICATION_ASSISTANCE = "CLARIFICATION_ASSISTANCE"


class RunEventType(StrEnum):
    """Semantic event families implemented by the V0.18 first slice."""

    REQUIREMENT_SUBMISSION_ACCEPTED = "REQUIREMENT_SUBMISSION_ACCEPTED"
    BROWNFIELD_BASELINE_SELECTED = "BROWNFIELD_BASELINE_SELECTED"
    BROWNFIELD_BASELINE_VERIFIED = "BROWNFIELD_BASELINE_VERIFIED"
    REQUIREMENT_ANALYSIS_REVIEW_DECIDED = (
        "REQUIREMENT_ANALYSIS_REVIEW_DECIDED"
    )
    TASK_GRAPH_REVIEW_DECIDED = "TASK_GRAPH_REVIEW_DECIDED"
    CLARIFICATION_DRAFT_REQUESTED = "CLARIFICATION_DRAFT_REQUESTED"
    CLARIFICATION_DRAFT_GENERATED = "CLARIFICATION_DRAFT_GENERATED"


class RunEventCorrelation(BaseModel):
    """Stable semantic anchors used for correlation and idempotency."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    requirement_submission_sha256: str | None = None
    baseline_id: str | None = None
    analysis_revision: int | None = Field(default=None, ge=0)
    task_graph_revision: int | None = Field(default=None, ge=0)
    review_sequence: int | None = Field(default=None, ge=1)
    generation_id: str | None = None
    context_identity: str | None = None

    @field_validator(
        "requirement_submission_sha256",
        "baseline_id",
        "generation_id",
        "context_identity",
    )
    @classmethod
    def validate_nonblank_optional_identity(cls, value: str | None) -> str | None:
        if value is not None and not value.strip():
            raise ValueError("Run-event correlation identities must be nonblank.")
        return value


class RunEventEvidenceReference(BaseModel):
    """Reference to existing structured authority or operational evidence."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    kind: str = Field(min_length=1)
    identity: str = Field(min_length=1)
    location: str = Field(min_length=1)

    @field_validator("kind", "identity", "location")
    @classmethod
    def validate_nonblank_reference(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("Run-event evidence references must be nonblank.")
        return value


RunEventDataValue: TypeAlias = str | int | bool | None


class RunEventDraft(BaseModel):
    """Complete semantic content before append assigns order and audit time."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_version: Literal["run-event-v1"]
    event_id: str
    run_id: str = Field(min_length=1)
    event_type: RunEventType
    actor: RunEventActor
    authority: RunEventAuthority
    stage: RunEventStage
    correlation: RunEventCorrelation
    data: dict[str, RunEventDataValue]
    evidence_refs: tuple[RunEventEvidenceReference, ...]

    @field_validator("event_id")
    @classmethod
    def validate_event_id(cls, value: str) -> str:
        suffix = value.removeprefix(_EVENT_ID_PREFIX)
        if (
            not value.startswith(_EVENT_ID_PREFIX)
            or len(suffix) != 24
            or any(character not in "0123456789ABCDEF" for character in suffix)
        ):
            raise ValueError("Run-event identity is not canonical.")
        return value

    @field_validator("run_id")
    @classmethod
    def validate_run_id(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("Run-event run_id must be nonblank.")
        return value

    @model_validator(mode="after")
    def validate_semantics(self) -> RunEventDraft:
        expected_actor, expected_authority, expected_stage = _EVENT_CLASSIFICATION[
            self.event_type
        ]
        if (
            self.actor is not expected_actor
            or self.authority is not expected_authority
            or self.stage is not expected_stage
        ):
            raise ValueError("Run-event actor, authority, or stage is inconsistent.")
        _validate_event_data(self.event_type, self.data)
        if not self.evidence_refs:
            raise ValueError("Semantic run events require an evidence reference.")
        expected_id = stable_run_event_id(
            self.run_id,
            self.event_type,
            *_event_identity_anchors(self.event_type, self.correlation),
        )
        if self.event_id != expected_id:
            raise ValueError("Run-event identity does not match its stable anchors.")
        return self


class RunEvent(RunEventDraft):
    """One fully ordered persisted semantic run event."""

    sequence: int = Field(ge=1)
    recorded_at: datetime

    @field_validator("recorded_at")
    @classmethod
    def validate_utc_timestamp(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() != UTC.utcoffset(value):
            raise ValueError("Run-event recorded_at must be timezone-aware UTC.")
        return value.astimezone(UTC)

    @field_serializer("recorded_at")
    def serialize_recorded_at(self, value: datetime) -> str:
        return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


@dataclass(frozen=True, slots=True)
class RunEventAppendResult:
    """Result of an append or an idempotent semantic replay."""

    event: RunEvent
    appended: bool


RunEventClock = Callable[[], datetime]


class RunEventLog:
    """Process-local, thread-safe append/read boundary for one run JSONL file.

    The implementation deliberately does not claim cross-process coordination.
    Every append validates the complete prior stream before assigning the next
    sequence number.
    """

    def __init__(
        self,
        bundle: LiveRunArtifactBundle,
        *,
        clock: RunEventClock | None = None,
    ) -> None:
        self._bundle = bundle
        self._clock = clock or (lambda: datetime.now(UTC))
        self._lock = Lock()

    @property
    def path(self) -> Path:
        """Return the live root event path, outside the frozen artifact bundle."""

        return self._bundle.run_events_path

    def read(self) -> tuple[RunEvent, ...]:
        """Read and validate every physical JSONL record in canonical order."""

        with self._lock:
            return self._read_locked()

    def append(self, draft: RunEventDraft) -> RunEventAppendResult:
        """Append once, no-op identical replay, or fail a conflicting replay."""

        if draft.run_id != self._bundle.run_id:
            raise RunEventIntegrityError(
                "A run event cannot be appended to another run's stream."
            )
        with self._lock:
            events = self._read_locked()
            for existing in events:
                if existing.event_id != draft.event_id:
                    continue
                if _semantic_payload(existing) == _semantic_payload(draft):
                    return RunEventAppendResult(event=existing, appended=False)
                raise RunEventConflictError(
                    f"Run-event identity has conflicting semantic content: "
                    f"{draft.event_id}"
                )

            recorded_at = self._clock()
            event = RunEvent(
                **draft.model_dump(mode="python"),
                sequence=len(events) + 1,
                recorded_at=recorded_at,
            )
            line = render_run_event_line(event).encode("utf-8")
            self._append_bytes_locked(line)
            return RunEventAppendResult(event=event, appended=True)

    def _read_locked(self) -> tuple[RunEvent, ...]:
        self._validate_owned_directories(create=False)
        path = self.path
        try:
            status = path.lstat()
        except FileNotFoundError:
            return ()
        except OSError as error:
            raise RunEventIntegrityError(
                "Run-event stream could not be inspected."
            ) from error
        if stat.S_ISLNK(status.st_mode) or not stat.S_ISREG(status.st_mode):
            raise RunEventIntegrityError(
                "Run-event stream must be a direct regular file."
            )
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(path, flags)
            with os.fdopen(descriptor, "rb") as stream:
                contents = stream.read()
        except OSError as error:
            raise RunEventIntegrityError(
                "Run-event stream could not be read safely."
            ) from error
        if not contents:
            return ()
        if not contents.endswith(b"\n"):
            raise RunEventIntegrityError(
                "Run-event stream ends with a malformed or truncated record."
            )
        try:
            text = contents.decode("utf-8")
        except UnicodeDecodeError as error:
            raise RunEventIntegrityError(
                "Run-event stream is not valid UTF-8."
            ) from error

        events: list[RunEvent] = []
        identities: set[str] = set()
        for expected_sequence, physical_line in enumerate(
            text.splitlines(), start=1
        ):
            if not physical_line:
                raise RunEventIntegrityError(
                    "Run-event stream contains an empty physical record."
                )
            try:
                event = RunEvent.model_validate_json(physical_line)
            except (TypeError, ValueError) as error:
                raise RunEventIntegrityError(
                    f"Run-event record {expected_sequence} is invalid."
                ) from error
            if render_run_event_line(event).removesuffix("\n") != physical_line:
                raise RunEventIntegrityError(
                    f"Run-event record {expected_sequence} is not canonical JSON."
                )
            if event.run_id != self._bundle.run_id:
                raise RunEventIntegrityError(
                    "Run-event stream contains an event owned by another run."
                )
            if event.sequence != expected_sequence:
                raise RunEventIntegrityError(
                    "Run-event sequence is not contiguous and monotonic."
                )
            if event.event_id in identities:
                raise RunEventIntegrityError(
                    "Run-event stream contains a duplicate event identity."
                )
            identities.add(event.event_id)
            events.append(event)
        return tuple(events)

    def _append_bytes_locked(self, contents: bytes) -> None:
        self._validate_owned_directories(create=True)
        path = self.path
        try:
            status = path.lstat()
        except FileNotFoundError:
            status = None
        except OSError as error:
            raise RunEventIntegrityError(
                "Run-event stream could not be inspected before append."
            ) from error
        if status is not None and (
            stat.S_ISLNK(status.st_mode) or not stat.S_ISREG(status.st_mode)
        ):
            raise RunEventIntegrityError(
                "Run-event stream must remain a direct regular file."
            )
        flags = os.O_WRONLY | os.O_APPEND | os.O_CREAT | getattr(
            os, "O_NOFOLLOW", 0
        )
        try:
            descriptor = os.open(path, flags, 0o600)
            try:
                written = os.write(descriptor, contents)
                if written != len(contents):
                    raise OSError("incomplete run-event record write")
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
        except OSError as error:
            raise RunEventError("Run-event append failed.") from error

    def _validate_owned_directories(self, *, create: bool) -> None:
        repository_root = self._bundle.repository_root
        runs_root = repository_root / RUNS_DIRECTORY_NAME
        run_root = self._bundle.run_root
        if create:
            _ensure_direct_directory(runs_root, parent=repository_root)
            _ensure_direct_directory(run_root, parent=runs_root)
            return
        if not _path_present(runs_root):
            return
        _require_direct_directory(runs_root, parent=repository_root)
        if not _path_present(run_root):
            return
        _require_direct_directory(run_root, parent=runs_root)


_EVENT_CLASSIFICATION = {
    RunEventType.REQUIREMENT_SUBMISSION_ACCEPTED: (
        RunEventActor.HUMAN,
        RunEventAuthority.HUMAN_INPUT,
        RunEventStage.REQUIREMENT_SUBMISSION,
    ),
    RunEventType.BROWNFIELD_BASELINE_SELECTED: (
        RunEventActor.HUMAN,
        RunEventAuthority.HUMAN_INPUT,
        RunEventStage.BROWNFIELD_BASELINE,
    ),
    RunEventType.BROWNFIELD_BASELINE_VERIFIED: (
        RunEventActor.SYSTEM,
        RunEventAuthority.AUTOMATED_CONSEQUENCE,
        RunEventStage.BROWNFIELD_BASELINE,
    ),
    RunEventType.REQUIREMENT_ANALYSIS_REVIEW_DECIDED: (
        RunEventActor.HUMAN,
        RunEventAuthority.HUMAN_GOVERNANCE,
        RunEventStage.REQUIREMENT_ANALYSIS,
    ),
    RunEventType.TASK_GRAPH_REVIEW_DECIDED: (
        RunEventActor.HUMAN,
        RunEventAuthority.HUMAN_GOVERNANCE,
        RunEventStage.TASK_GRAPH,
    ),
    RunEventType.CLARIFICATION_DRAFT_REQUESTED: (
        RunEventActor.HUMAN,
        RunEventAuthority.NON_AUTHORITATIVE_ASSISTANCE,
        RunEventStage.CLARIFICATION_ASSISTANCE,
    ),
    RunEventType.CLARIFICATION_DRAFT_GENERATED: (
        RunEventActor.AI_ASSISTANT,
        RunEventAuthority.NON_AUTHORITATIVE_ASSISTANCE,
        RunEventStage.CLARIFICATION_ASSISTANCE,
    ),
}


_EVENT_DATA_FIELDS = {
    RunEventType.REQUIREMENT_SUBMISSION_ACCEPTED: {
        "source_kind",
        "original_sha256",
        "normalized_sha256",
        "source_filename",
    },
    RunEventType.BROWNFIELD_BASELINE_SELECTED: {
        "selected_project_name",
        "originating_run_id",
        "publication_bundle_sha256",
        "source_snapshot_id",
    },
    RunEventType.BROWNFIELD_BASELINE_VERIFIED: {
        "baseline_id",
        "governed_baseline_snapshot_id",
        "codebase_context_id",
        "verified",
    },
    RunEventType.REQUIREMENT_ANALYSIS_REVIEW_DECIDED: {
        "decision",
        "feedback_present",
        "feedback_sha256",
        "revision_number",
        "review_sequence",
    },
    RunEventType.TASK_GRAPH_REVIEW_DECIDED: {
        "decision",
        "feedback_present",
        "feedback_sha256",
        "revision_number",
        "review_sequence",
    },
    RunEventType.CLARIFICATION_DRAFT_REQUESTED: {
        "analysis_revision",
        "generation_id",
        "context_identity",
        "model_name",
    },
    RunEventType.CLARIFICATION_DRAFT_GENERATED: {
        "analysis_revision",
        "generation_id",
        "context_identity",
        "model_name",
        "draft_sha256",
        "character_count",
        "availability_status",
    },
}


def stable_run_event_id(
    run_id: str,
    event_type: RunEventType,
    *anchors: str,
) -> str:
    """Derive a stable identity from semantic anchors, never append sequence."""

    if not run_id.strip() or not anchors or any(not anchor for anchor in anchors):
        raise ValueError("Stable run-event identity requires nonblank anchors.")
    payload = {
        "schema_version": RUN_EVENT_SCHEMA_VERSION,
        "run_id": run_id,
        "event_type": event_type.value,
        "anchors": list(anchors),
    }
    digest = hashlib.sha256(_canonical_json(payload).encode("utf-8")).hexdigest()
    return _EVENT_ID_PREFIX + digest[:24].upper()


def render_run_event_line(event: RunEvent) -> str:
    """Serialize exactly one canonical JSON object and one physical newline."""

    return _canonical_json(event.model_dump(mode="json")) + "\n"


def build_authoritative_run_event_drafts(
    state: Mapping[str, object],
) -> tuple[RunEventDraft, ...]:
    """Project reconstructible semantic events from authoritative workflow state."""

    run_id = _required_text(state.get("run_id"), "run ID")
    drafts: list[RunEventDraft] = []
    submission_value = state.get("requirement_submission")
    if submission_value is not None:
        submission = _required_mapping(submission_value, "requirement submission")
        normalized_sha256 = _required_sha256(
            submission.get("normalized_sha256"), "normalized requirement hash"
        )
        original_sha256 = _required_sha256(
            submission.get("original_sha256"), "original requirement hash"
        )
        source_kind = _required_text(submission.get("source_kind"), "source kind")
        source_filename_value = submission.get("source_filename")
        if source_filename_value is not None and not isinstance(
            source_filename_value, str
        ):
            raise RunEventIntegrityError(
                "Requirement submission source filename is invalid."
            )
        correlation = RunEventCorrelation(
            requirement_submission_sha256=normalized_sha256
        )
        drafts.append(
            _draft(
                run_id,
                RunEventType.REQUIREMENT_SUBMISSION_ACCEPTED,
                correlation,
                data={
                    "source_kind": source_kind,
                    "original_sha256": original_sha256,
                    "normalized_sha256": normalized_sha256,
                    "source_filename": source_filename_value,
                },
                evidence_refs=(
                    RunEventEvidenceReference(
                        kind="REQUIREMENT_SUBMISSION",
                        identity=normalized_sha256,
                        location="workflow_state.requirement_submission",
                    ),
                ),
            )
        )

    baseline_value = state.get("brownfield_baseline")
    context_value = state.get("brownfield_codebase_context")
    if baseline_value is not None or context_value is not None:
        if baseline_value is None or context_value is None:
            raise RunEventIntegrityError(
                "Brownfield baseline and codebase context are incomplete."
            )
        try:
            baseline = BrownfieldBaselineProvenance.model_validate(
                dict(_required_mapping(baseline_value, "brownfield baseline")),
                strict=False,
            )
            context = BrownfieldCodebaseContext.model_validate(
                dict(_required_mapping(context_value, "brownfield codebase context")),
                strict=False,
            )
        except (TypeError, ValueError) as error:
            raise RunEventIntegrityError(
                "Brownfield baseline evidence is invalid."
            ) from error
        if (
            context.baseline_id != baseline.baseline_id
            or context.selected_project_name != baseline.selected_project_name
            or context.binding.workspace_id != baseline.seed_result.workspace_id
            or context.binding.snapshot_id
            != baseline.governed_baseline_snapshot_id
        ):
            raise RunEventIntegrityError(
                "Brownfield baseline verification does not correlate."
            )
        correlation = RunEventCorrelation(baseline_id=baseline.baseline_id)
        drafts.extend(
            (
                _draft(
                    run_id,
                    RunEventType.BROWNFIELD_BASELINE_SELECTED,
                    correlation,
                    data={
                        "selected_project_name": baseline.selected_project_name,
                        "originating_run_id": baseline.originating_run_id,
                        "publication_bundle_sha256": (
                            baseline.publication_bundle_sha256
                        ),
                        "source_snapshot_id": baseline.source_snapshot_id,
                    },
                    evidence_refs=(
                        RunEventEvidenceReference(
                            kind="BROWNFIELD_BASELINE_PROVENANCE",
                            identity=baseline.baseline_id,
                            location="workflow_state.brownfield_baseline",
                        ),
                    ),
                ),
                _draft(
                    run_id,
                    RunEventType.BROWNFIELD_BASELINE_VERIFIED,
                    correlation,
                    data={
                        "baseline_id": baseline.baseline_id,
                        "governed_baseline_snapshot_id": (
                            baseline.governed_baseline_snapshot_id
                        ),
                        "codebase_context_id": context.context_id,
                        "verified": True,
                    },
                    evidence_refs=(
                        RunEventEvidenceReference(
                            kind="BROWNFIELD_BASELINE_PROVENANCE",
                            identity=baseline.baseline_id,
                            location="workflow_state.brownfield_baseline",
                        ),
                        RunEventEvidenceReference(
                            kind="BROWNFIELD_CODEBASE_CONTEXT",
                            identity=context.context_id,
                            location="workflow_state.brownfield_codebase_context",
                        ),
                    ),
                ),
            )
        )

    drafts.extend(
        _review_event_drafts(
            run_id,
            state,
            event_type=RunEventType.REQUIREMENT_ANALYSIS_REVIEW_DECIDED,
            history_name="requirement_review_history",
            revision_history_name="requirement_analysis_history",
            revision_data_name="analysis",
        )
    )
    drafts.extend(
        _review_event_drafts(
            run_id,
            state,
            event_type=RunEventType.TASK_GRAPH_REVIEW_DECIDED,
            history_name="task_graph_review_history",
            revision_history_name="task_graph_history",
            revision_data_name="task_graph",
        )
    )
    return tuple(drafts)


def build_clarification_draft_requested_event(
    request: ClarificationDraftRequest,
    *,
    generation_id: str,
    context_identity: str,
    model_name: str,
) -> RunEventDraft:
    """Describe an explicit human request for non-authoritative assistance."""

    _validate_clarification_identity(request, generation_id, context_identity)
    model = _required_text(model_name, "clarification model")
    correlation = RunEventCorrelation(
        analysis_revision=request.analysis_revision,
        generation_id=generation_id,
        context_identity=context_identity,
    )
    return _draft(
        request.run_id,
        RunEventType.CLARIFICATION_DRAFT_REQUESTED,
        correlation,
        data={
            "analysis_revision": request.analysis_revision,
            "generation_id": generation_id,
            "context_identity": context_identity,
            "model_name": model,
        },
        evidence_refs=(
            RunEventEvidenceReference(
                kind="REQUIREMENT_ANALYSIS_REVISION",
                identity=f"revision:{request.analysis_revision}",
                location="workflow_state.requirement_analysis_history",
            ),
        ),
    )


def build_clarification_draft_generated_event(
    request: ClarificationDraftRequest,
    result: ClarificationDraftResult,
    *,
    generation_id: str,
    context_identity: str,
    model_name: str,
) -> RunEventDraft:
    """Describe a current AI draft becoming available for human review."""

    _validate_clarification_identity(request, generation_id, context_identity)
    model = _required_text(model_name, "clarification model")
    draft_text = result.suggested_clarification
    correlation = RunEventCorrelation(
        analysis_revision=request.analysis_revision,
        generation_id=generation_id,
        context_identity=context_identity,
    )
    requested_id = stable_run_event_id(
        request.run_id,
        RunEventType.CLARIFICATION_DRAFT_REQUESTED,
        generation_id,
        context_identity,
    )
    return _draft(
        request.run_id,
        RunEventType.CLARIFICATION_DRAFT_GENERATED,
        correlation,
        data={
            "analysis_revision": request.analysis_revision,
            "generation_id": generation_id,
            "context_identity": context_identity,
            "model_name": model,
            "draft_sha256": hashlib.sha256(draft_text.encode("utf-8")).hexdigest(),
            "character_count": len(draft_text),
            "availability_status": "AVAILABLE_FOR_HUMAN_REVIEW",
        },
        evidence_refs=(
            RunEventEvidenceReference(
                kind="CLARIFICATION_DRAFT_REQUEST",
                identity=requested_id,
                location=RUN_EVENTS_FILENAME,
            ),
            RunEventEvidenceReference(
                kind="REQUIREMENT_ANALYSIS_REVISION",
                identity=f"revision:{request.analysis_revision}",
                location="workflow_state.requirement_analysis_history",
            ),
        ),
    )


def _review_event_drafts(
    run_id: str,
    state: Mapping[str, object],
    *,
    event_type: RunEventType,
    history_name: str,
    revision_history_name: str,
    revision_data_name: str,
) -> tuple[RunEventDraft, ...]:
    history = _mapping_sequence(state.get(history_name, ()), history_name)
    revision_history = _mapping_sequence(
        state.get(revision_history_name, ()), revision_history_name
    )
    drafts: list[RunEventDraft] = []
    for expected_sequence, review in enumerate(history, start=1):
        sequence = _required_int(review.get("sequence"), "review sequence", minimum=1)
        if sequence != expected_sequence:
            raise RunEventIntegrityError(
                f"{history_name} is not contiguous and ordered."
            )
        revision = _required_int(
            review.get("revision_number"), "review revision", minimum=0
        )
        decision = _required_text(review.get("decision"), "review decision")
        if decision not in {"APPROVE", "REQUEST_CHANGES", "REJECT"}:
            raise RunEventIntegrityError("Review decision is not canonical.")
        feedback = review.get("feedback")
        if not isinstance(feedback, str):
            raise RunEventIntegrityError("Authoritative review feedback is invalid.")
        revision_records = tuple(
            record
            for record in revision_history
            if record.get("revision_number") == revision
        )
        if len(revision_records) != 1:
            raise RunEventIntegrityError(
                f"Review revision {revision} has no exact authoritative record."
            )
        revision_record = revision_records[0]
        revision_sequence = _required_int(
            revision_record.get("sequence"), "revision-history sequence", minimum=1
        )
        revision_data = _required_mapping(
            revision_record.get(revision_data_name), revision_data_name
        )
        revision_identity = _review_revision_identity(
            event_type,
            revision_data,
            revision=revision,
        )
        correlation = RunEventCorrelation(
            analysis_revision=(
                revision
                if event_type
                is RunEventType.REQUIREMENT_ANALYSIS_REVIEW_DECIDED
                else None
            ),
            task_graph_revision=(
                revision
                if event_type is RunEventType.TASK_GRAPH_REVIEW_DECIDED
                else None
            ),
            review_sequence=sequence,
        )
        feedback_present = bool(feedback)
        feedback_sha256 = (
            hashlib.sha256(feedback.encode("utf-8")).hexdigest()
            if feedback_present
            else None
        )
        drafts.append(
            _draft(
                run_id,
                event_type,
                correlation,
                data={
                    "decision": decision,
                    "feedback_present": feedback_present,
                    "feedback_sha256": feedback_sha256,
                    "revision_number": revision,
                    "review_sequence": sequence,
                },
                evidence_refs=(
                    RunEventEvidenceReference(
                        kind=(
                            "REQUIREMENT_ANALYSIS_REVIEW_HISTORY"
                            if event_type
                            is RunEventType.REQUIREMENT_ANALYSIS_REVIEW_DECIDED
                            else "TASK_GRAPH_REVIEW_HISTORY"
                        ),
                        identity=f"sequence:{sequence}",
                        location=f"workflow_state.{history_name}",
                    ),
                    RunEventEvidenceReference(
                        kind=(
                            "REQUIREMENT_ANALYSIS_REVISION"
                            if event_type
                            is RunEventType.REQUIREMENT_ANALYSIS_REVIEW_DECIDED
                            else "TASK_GRAPH_REVISION"
                        ),
                        identity=revision_identity,
                        location=(
                            f"workflow_state.{revision_history_name}"
                            f"[sequence:{revision_sequence}]"
                        ),
                    ),
                ),
            )
        )
    return tuple(drafts)


def _review_revision_identity(
    event_type: RunEventType,
    revision_data: Mapping[str, object],
    *,
    revision: int,
) -> str:
    if event_type is RunEventType.REQUIREMENT_ANALYSIS_REVIEW_DECIDED:
        return f"analysis-revision:{revision}"
    return _required_text(revision_data.get("graph_id"), "TaskGraph identity")


def _draft(
    run_id: str,
    event_type: RunEventType,
    correlation: RunEventCorrelation,
    *,
    data: dict[str, RunEventDataValue],
    evidence_refs: tuple[RunEventEvidenceReference, ...],
) -> RunEventDraft:
    actor, authority, stage = _EVENT_CLASSIFICATION[event_type]
    return RunEventDraft(
        schema_version=RUN_EVENT_SCHEMA_VERSION,
        event_id=stable_run_event_id(
            run_id,
            event_type,
            *_event_identity_anchors(event_type, correlation),
        ),
        run_id=run_id,
        event_type=event_type,
        actor=actor,
        authority=authority,
        stage=stage,
        correlation=correlation,
        data=data,
        evidence_refs=evidence_refs,
    )


def _event_identity_anchors(
    event_type: RunEventType,
    correlation: RunEventCorrelation,
) -> tuple[str, ...]:
    if event_type is RunEventType.REQUIREMENT_SUBMISSION_ACCEPTED:
        return (
            _required_text(
                correlation.requirement_submission_sha256,
                "requirement-submission correlation",
            ),
        )
    if event_type in {
        RunEventType.BROWNFIELD_BASELINE_SELECTED,
        RunEventType.BROWNFIELD_BASELINE_VERIFIED,
    }:
        return (_required_text(correlation.baseline_id, "baseline correlation"),)
    if event_type in {
        RunEventType.REQUIREMENT_ANALYSIS_REVIEW_DECIDED,
        RunEventType.TASK_GRAPH_REVIEW_DECIDED,
    }:
        sequence = correlation.review_sequence
        if sequence is None:
            raise ValueError("Review events require a review-history sequence.")
        return (str(sequence),)
    return (
        _required_text(correlation.generation_id, "clarification generation"),
        _required_text(correlation.context_identity, "clarification context"),
    )


def _validate_event_data(
    event_type: RunEventType,
    data: Mapping[str, RunEventDataValue],
) -> None:
    if set(data) != _EVENT_DATA_FIELDS[event_type]:
        raise ValueError("Run-event data fields do not match the event contract.")
    if event_type in {
        RunEventType.REQUIREMENT_ANALYSIS_REVIEW_DECIDED,
        RunEventType.TASK_GRAPH_REVIEW_DECIDED,
    }:
        if data["decision"] not in {"APPROVE", "REQUEST_CHANGES", "REJECT"}:
            raise ValueError("Run-event review decision is invalid.")
        if not isinstance(data["feedback_present"], bool):
            raise ValueError("Run-event feedback_present must be boolean.")
        feedback_hash = data["feedback_sha256"]
        if data["feedback_present"]:
            _required_sha256(feedback_hash, "run-event feedback hash")
        elif feedback_hash is not None:
            raise ValueError("Absent feedback must not carry a feedback hash.")
    if event_type is RunEventType.BROWNFIELD_BASELINE_VERIFIED and (
        data["verified"] is not True
    ):
        raise ValueError("Baseline verification events must record verified=true.")
    if event_type is RunEventType.CLARIFICATION_DRAFT_GENERATED:
        if data["availability_status"] != "AVAILABLE_FOR_HUMAN_REVIEW":
            raise ValueError("Clarification availability status is invalid.")
        _required_sha256(data["draft_sha256"], "clarification draft hash")
        count = data["character_count"]
        if not isinstance(count, int) or isinstance(count, bool) or count < 1:
            raise ValueError("Clarification character count must be positive.")


def _validate_clarification_identity(
    request: ClarificationDraftRequest,
    generation_id: str,
    context_identity: str,
) -> None:
    _required_text(generation_id, "clarification generation")
    _required_text(context_identity, "clarification context")
    if clarification_draft_context_identity(request) != context_identity:
        raise ValueError("Clarification context identity does not match its request.")


def _semantic_payload(event: RunEventDraft) -> dict[str, object]:
    return event.model_dump(
        mode="json",
        exclude={"sequence", "recorded_at"},
    )


def _canonical_json(value: object) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def _required_mapping(value: object, label: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise RunEventIntegrityError(f"{label} must be structured evidence.")
    return cast(Mapping[str, object], value)


def _mapping_sequence(
    value: object,
    label: str,
) -> tuple[Mapping[str, object], ...]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        raise RunEventIntegrityError(f"{label} must be an ordered evidence list.")
    return tuple(_required_mapping(item, label) for item in value)


def _required_text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise RunEventIntegrityError(f"{label} must be nonblank text.")
    return value


def _required_int(value: object, label: str, *, minimum: int) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < minimum:
        raise RunEventIntegrityError(f"{label} is invalid.")
    return value


def _required_sha256(value: object, label: str) -> str:
    text = _required_text(value, label)
    if len(text) != _SHA256_LENGTH or any(
        character not in "0123456789abcdef" for character in text
    ):
        raise RunEventIntegrityError(f"{label} must be lowercase SHA-256.")
    return text


def _ensure_direct_directory(path: Path, *, parent: Path) -> None:
    try:
        path.mkdir()
    except FileExistsError:
        pass
    except OSError as error:
        raise RunEventIntegrityError(
            "Run-event directory could not be created safely."
        ) from error
    _require_direct_directory(path, parent=parent)


def _require_direct_directory(path: Path, *, parent: Path) -> None:
    try:
        status = path.lstat()
        resolved = path.resolve(strict=True)
    except OSError as error:
        raise RunEventIntegrityError(
            "Run-event directory could not be verified."
        ) from error
    if (
        stat.S_ISLNK(status.st_mode)
        or not stat.S_ISDIR(status.st_mode)
        or resolved != path
        or resolved.parent != parent
    ):
        raise RunEventIntegrityError(
            "Run-event directory must be a direct canonical directory."
        )


def _path_present(path: Path) -> bool:
    try:
        path.lstat()
    except FileNotFoundError:
        return False
    except OSError as error:
        raise RunEventIntegrityError(
            "Run-event path could not be inspected."
        ) from error
    return True
