"""Focused invariants for append-only semantic run events."""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path

import pytest

from agentic_sdlc.clarification_draft import (
    ClarificationDraftResult,
    clarification_draft_context_identity,
)
from agentic_sdlc.run_artifacts import LiveRunArtifactBundle
from agentic_sdlc.run_events import (
    RUN_EVENT_SCHEMA_VERSION,
    RunEventActor,
    RunEventAuthority,
    RunEventConflictError,
    RunEventDraft,
    RunEventIntegrityError,
    RunEventLog,
    RunEventType,
    build_clarification_draft_generated_event,
    build_clarification_draft_requested_event,
)
from tests.test_clarification_draft import _request


FIXED_TIME = datetime(2026, 8, 16, 18, 0, tzinfo=UTC)


def _log(tmp_path: Path) -> RunEventLog:
    return RunEventLog(
        LiveRunArtifactBundle.under_repository(tmp_path, "run-clarification"),
        clock=lambda: FIXED_TIME,
    )


def _requested(generation_id: str = "generation-1") -> RunEventDraft:
    request = _request()
    context = clarification_draft_context_identity(request)
    return build_clarification_draft_requested_event(
        request,
        generation_id=generation_id,
        context_identity=context,
        model_name="test-model",
    )


def test_writer_emits_one_canonical_object_per_line_and_never_rewrites_prefix(
    tmp_path: Path,
) -> None:
    log = _log(tmp_path)
    request = _request()
    context = clarification_draft_context_identity(request)
    requested = _requested()
    generated = build_clarification_draft_generated_event(
        request,
        ClarificationDraftResult(suggested_clarification="Retain notes for 30 days."),
        generation_id="generation-1",
        context_identity=context,
        model_name="test-model",
    )

    first = log.append(requested)
    prefix = log.path.read_bytes()
    second = log.append(generated)
    contents = log.path.read_bytes()

    assert first.appended and second.appended
    assert contents.startswith(prefix)
    assert contents.count(b"\n") == 2
    documents = [json.loads(line) for line in contents.decode().splitlines()]
    assert documents[0]["schema_version"] == RUN_EVENT_SCHEMA_VERSION
    assert documents[0]["sequence"] == 1
    assert documents[1]["sequence"] == 2
    assert documents[0]["recorded_at"] == "2026-08-16T18:00:00Z"
    assert set(documents[0]) == {
        "actor",
        "authority",
        "correlation",
        "data",
        "event_id",
        "event_type",
        "evidence_refs",
        "recorded_at",
        "run_id",
        "schema_version",
        "sequence",
        "stage",
    }
    assert log.read() == (first.event, second.event)


def test_identical_replay_is_noop_and_conflicting_identity_fails(
    tmp_path: Path,
) -> None:
    log = _log(tmp_path)
    draft = _requested()
    first = log.append(draft)
    original = log.path.read_bytes()

    replay = log.append(draft)
    changed_data = dict(draft.data)
    changed_data["model_name"] = "different-model"
    conflict = RunEventDraft.model_validate(
        {**draft.model_dump(mode="python"), "data": changed_data}
    )

    assert not replay.appended
    assert replay.event == first.event
    assert log.path.read_bytes() == original
    with pytest.raises(RunEventConflictError, match="conflicting semantic content"):
        log.append(conflict)
    assert log.path.read_bytes() == original


@pytest.mark.parametrize(
    "contents",
    (
        b'{"truncated":true}',
        b"not-json\n",
        b"\n",
    ),
)
def test_malformed_or_truncated_existing_stream_is_rejected(
    tmp_path: Path,
    contents: bytes,
) -> None:
    log = _log(tmp_path)
    log.path.parent.mkdir(parents=True)
    log.path.write_bytes(contents)

    with pytest.raises(RunEventIntegrityError):
        log.read()
    with pytest.raises(RunEventIntegrityError):
        log.append(_requested())


def test_threaded_process_local_appends_are_contiguous_and_unique(
    tmp_path: Path,
) -> None:
    log = _log(tmp_path)
    drafts = tuple(_requested(f"generation-{index}") for index in range(1, 33))

    with ThreadPoolExecutor(max_workers=8) as executor:
        results = tuple(executor.map(log.append, drafts))

    events = log.read()
    assert all(result.appended for result in results)
    assert [event.sequence for event in events] == list(range(1, 33))
    assert len({event.event_id for event in events}) == 32
    assert log.path.read_bytes().count(b"\n") == 32


def test_symlinked_run_root_or_stream_is_rejected(tmp_path: Path) -> None:
    bundle = LiveRunArtifactBundle.under_repository(tmp_path, "run-clarification")
    outside = tmp_path / "outside"
    outside.mkdir()
    (tmp_path / "runs").mkdir()
    bundle.run_root.symlink_to(outside, target_is_directory=True)

    with pytest.raises(RunEventIntegrityError, match="direct canonical directory"):
        RunEventLog(bundle).append(_requested())

    bundle.run_root.unlink()
    bundle.run_root.mkdir()
    target = outside / "events.jsonl"
    target.write_text("", encoding="utf-8")
    bundle.run_events_path.symlink_to(target)
    with pytest.raises(RunEventIntegrityError, match="direct regular file"):
        RunEventLog(bundle).append(_requested())


def test_clarification_events_keep_human_and_ai_non_authoritative_and_omit_text(
) -> None:
    request = _request()
    context = clarification_draft_context_identity(request)
    result = ClarificationDraftResult(
        suggested_clarification="Users can delete notes after authentication."
    )
    requested = build_clarification_draft_requested_event(
        request,
        generation_id="generation-1",
        context_identity=context,
        model_name="test-model",
    )
    generated = build_clarification_draft_generated_event(
        request,
        result,
        generation_id="generation-1",
        context_identity=context,
        model_name="test-model",
    )

    assert requested.event_type is RunEventType.CLARIFICATION_DRAFT_REQUESTED
    assert requested.actor is RunEventActor.HUMAN
    assert requested.authority is RunEventAuthority.NON_AUTHORITATIVE_ASSISTANCE
    assert generated.event_type is RunEventType.CLARIFICATION_DRAFT_GENERATED
    assert generated.actor is RunEventActor.AI_ASSISTANT
    assert generated.authority is RunEventAuthority.NON_AUTHORITATIVE_ASSISTANCE
    serialized = json.dumps(generated.model_dump(mode="json"))
    assert result.suggested_clarification not in serialized
    assert "suggested_clarification" not in serialized
    assert generated.data["draft_sha256"]
    assert generated.data["character_count"] == len(
        result.suggested_clarification
    )
